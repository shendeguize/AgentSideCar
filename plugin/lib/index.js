import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import z from "@deepseek-ai/schemastery";
import { createConnection } from "node:net";
import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
/** Title chars kept in prompts and logs (titles are untrusted input too). */
const MAX_TITLE_CHARS = 200;
/** Appended to the input text when it was cut at `maxInputChars`. */
const TRUNCATION_MARKER = "\n…[输入已截断 / input truncated]";
/** Honesty banner attached to every result (design §7-B / risk 12). */
const ANALYSIS_DISCLAIMER = "AI 分析仅供参考,由模型基于有界摘要推断生成,可能不完整或有误 / AI-generated analysis for reference only; inferred from a bounded summary and may be incomplete or wrong.";
/**
* Read-only-analyst guidance. `CreateAgentOptions` has no system-prompt field
* (d.ts fact above), so this rides the first user message.
*/
const ANALYSIS_GUIDANCE = [
	"你是只读分析助手:基于下面提供的 agent 会话摘要给出洞察(状态判断、异常与风险、可能的下一步建议)。",
	"不执行任何操作、不调用任何工具、不修改任何东西;不要假设摘要之外的事实,摘要可能不完整或被截断,不确定处请如实说明。",
	"You are a read-only analysis assistant: provide insights (state assessment, anomalies/risks, possible next steps) based solely on the agent-session summary below.",
	"Take no actions, call no tools, change nothing; do not assume facts beyond the summary — it may be incomplete or truncated, so state uncertainty honestly."
].join("\n");
function describeError$4(error) {
	return error instanceof Error ? error.message : String(error);
}
function boundText(text, maxChars) {
	if (text.length <= maxChars) return {
		text,
		truncated: false
	};
	return {
		text: text.slice(0, maxChars) + TRUNCATION_MARKER,
		truncated: true
	};
}
function boundTitle(title) {
	return title.length <= MAX_TITLE_CHARS ? title : title.slice(0, MAX_TITLE_CHARS) + "…";
}
/** Race a promise against a bounded timer; the timer is always cleared. */
async function withTimeout(promise, ms) {
	let timer;
	try {
		return await Promise.race([promise.then((value) => ({
			timedOut: false,
			value
		})), new Promise((resolve) => {
			timer = setTimeout(() => resolve({ timedOut: true }), Math.max(1, ms));
		})]);
	} finally {
		if (timer !== void 0) clearTimeout(timer);
	}
}
/** Join the text blocks of assistant messages appended after `baseline`. */
function extractNewAssistantText(messages, baseline) {
	const parts = [];
	for (const message of messages.slice(baseline)) {
		if (message.role !== "assistant") continue;
		const text = message.content.filter((block) => block.type === "text" && typeof block.text === "string").map((block) => block.text).join("\n");
		if (text.length > 0) parts.push(text);
	}
	return parts.join("\n\n");
}
/** Sum reported input+output tokens on new `assistant/message` events. */
function extractTokensHint(events, baseline) {
	let total = 0;
	let reported = false;
	for (const event of events.slice(baseline)) {
		if (event.type !== "assistant/message") continue;
		const usage = event.data?.usage;
		if (usage === void 0) continue;
		reported = true;
		if (typeof usage.inputTokens === "number") total += usage.inputTokens;
		if (typeof usage.outputTokens === "number") total += usage.outputTokens;
	}
	return reported ? total : void 0;
}
var AnalysisEngine = class {
	deps;
	now;
	maxInputChars;
	analysisTimeoutMs;
	maxActiveSessions;
	pluginName;
	active = /* @__PURE__ */ new Map();
	mintCounter = 0;
	constructor(deps) {
		this.deps = deps;
		this.now = deps.now ?? Date.now;
		this.maxInputChars = deps.maxInputChars ?? 8e3;
		this.analysisTimeoutMs = deps.analysisTimeoutMs ?? 6e4;
		this.maxActiveSessions = deps.maxActiveSessions ?? 4;
		this.pluginName = deps.pluginName ?? "agent-sidecar";
	}
	/** Number of live (or being-created) analysis sessions. */
	get activeCount() {
		return this.active.size;
	}
	/**
	* Start a dedicated analysis session and return its first insight.
	* Establishment (create + priming prompt + first response) shares one
	* `analysisTimeoutMs` budget; on timeout the turn is cancelled and the
	* session disposed, so a timed-out request leaves nothing running.
	*/
	async request(input) {
		const startedAt = this.now();
		const title = boundTitle(input.title);
		if (!this.deps.allowAnalysis()) return this.failResult("request", {
			kind: input.kind,
			title
		}, "analysis_disabled", {
			truncated: false,
			startedAt
		});
		if (this.active.size >= this.maxActiveSessions) return this.failResult("request", {
			kind: input.kind,
			title
		}, "too_many_active", {
			truncated: false,
			startedAt,
			detail: `active analyses at cap (${this.maxActiveSessions})`
		});
		const bounded = boundText(input.summaryText, this.maxInputChars);
		const analysisSessionId = this.mintSessionId();
		const entry = {
			analysisSessionId,
			kind: input.kind,
			title,
			busy: true,
			messageBaseline: 0,
			eventBaseline: 0,
			messageSeq: 0
		};
		this.active.set(analysisSessionId, entry);
		const deadline = startedAt + this.analysisTimeoutMs;
		const controller = new AbortController();
		let createTimedOut = false;
		const createPromise = this.deps.createAgent({
			sessionId: analysisSessionId,
			signal: controller.signal
		});
		createPromise.then((late) => {
			if (createTimedOut) late.dispose().catch(() => {});
		}, () => {});
		let handle;
		try {
			const created = await withTimeout(createPromise, deadline - this.now());
			if (created.timedOut) {
				createTimedOut = true;
				this.active.delete(analysisSessionId);
				controller.abort();
				return this.timeoutResult("request", entry, bounded.truncated, startedAt, void 0);
			}
			handle = created.value;
		} catch (error) {
			this.active.delete(analysisSessionId);
			return this.failResult("request", entry, "create_failed", {
				truncated: bounded.truncated,
				startedAt,
				detail: describeError$4(error)
			});
		}
		entry.handle = handle;
		this.deps.log({
			op: "create",
			analysisSessionId,
			kind: input.kind,
			title,
			truncated: bounded.truncated,
			inputChars: bounded.text.length
		});
		const prompt = this.buildInitialPrompt(input.kind, title, bounded);
		const turn = await this.runTurn(entry, prompt, deadline);
		entry.busy = false;
		if (turn.status === "threw") {
			this.active.delete(analysisSessionId);
			await this.disposeQuietly(entry);
			return this.failResult("request", entry, "create_failed", {
				truncated: bounded.truncated,
				startedAt,
				detail: turn.detail
			});
		}
		if (turn.status === "timeout") {
			this.cancelQuietly(entry);
			this.active.delete(analysisSessionId);
			await this.disposeQuietly(entry);
			return this.timeoutResult("request", entry, bounded.truncated, startedAt, void 0);
		}
		const result = {
			outcome: "completed",
			analysisSessionId,
			summary: turn.summary,
			truncated: bounded.truncated,
			...turn.tokensHint !== void 0 ? { tokensHint: turn.tokensHint } : {},
			disclaimer: ANALYSIS_DISCLAIMER
		};
		this.logResult("request", entry, result, startedAt);
		return result;
	}
	/**
	* Ask an incremental follow-up question on an established analysis session.
	* A timeout cancels the in-flight turn but KEEPS the session (its prior
	* context stays valuable; the UI may retry or cancel).
	*/
	async followup(analysisSessionId, question) {
		const startedAt = this.now();
		const entry = this.active.get(analysisSessionId);
		if (!this.deps.allowAnalysis()) return this.failResult("followup", entry ?? { analysisSessionId }, "analysis_disabled", {
			truncated: false,
			startedAt
		});
		if (entry === void 0 || entry.handle === void 0) return this.failResult("followup", { analysisSessionId }, "cancelled", {
			truncated: false,
			startedAt,
			detail: "unknown or already-cancelled analysis session"
		});
		if (entry.busy) return this.failResult("followup", entry, "too_many_active", {
			truncated: false,
			startedAt,
			detail: "a turn is already in flight on this analysis session"
		});
		const bounded = boundText(question, this.maxInputChars);
		this.deps.log({
			op: "followup",
			analysisSessionId,
			kind: entry.kind,
			title: entry.title,
			truncated: bounded.truncated,
			inputChars: bounded.text.length
		});
		entry.busy = true;
		try {
			const turn = await this.runTurn(entry, bounded.text, startedAt + this.analysisTimeoutMs);
			if (turn.status === "threw") {
				this.active.delete(analysisSessionId);
				await this.disposeQuietly(entry);
				return this.failResult("followup", entry, "cancelled", {
					truncated: bounded.truncated,
					startedAt,
					detail: turn.detail
				});
			}
			if (turn.status === "timeout") {
				this.cancelQuietly(entry);
				return this.timeoutResult("followup", entry, bounded.truncated, startedAt, analysisSessionId);
			}
			const result = {
				outcome: "completed",
				analysisSessionId,
				summary: turn.summary,
				truncated: bounded.truncated,
				...turn.tokensHint !== void 0 ? { tokensHint: turn.tokensHint } : {},
				disclaimer: ANALYSIS_DISCLAIMER
			};
			this.logResult("followup", entry, result, startedAt);
			return result;
		} finally {
			entry.busy = false;
		}
	}
	/**
	* Stop and dispose one analysis session (UI stop button). Idempotent: an
	* unknown id resolves as a logged no-op.
	*/
	async cancel(analysisSessionId) {
		const entry = this.active.get(analysisSessionId);
		if (entry === void 0) {
			this.deps.log({
				op: "cancel",
				analysisSessionId,
				found: false
			});
			return;
		}
		this.active.delete(analysisSessionId);
		this.cancelQuietly(entry);
		await this.disposeQuietly(entry);
		this.deps.log({
			op: "cancel",
			analysisSessionId,
			kind: entry.kind,
			title: entry.title,
			found: true
		});
	}
	async runTurn(entry, text, deadline) {
		const handle = entry.handle;
		const session = handle.agent.session;
		entry.messageBaseline = session.deriveMessages().length;
		entry.eventBaseline = session.events.length;
		const message = {
			id: `${entry.analysisSessionId}-msg-${++entry.messageSeq}`,
			role: "user",
			content: [{
				type: "text",
				text
			}],
			source: {
				kind: "plugin",
				plugin: this.pluginName
			}
		};
		try {
			handle.agent.followup(message);
		} catch (error) {
			return {
				status: "threw",
				detail: describeError$4(error)
			};
		}
		let idle;
		try {
			idle = await withTimeout(handle.agent.whenIdle(), deadline - this.now());
		} catch (error) {
			return {
				status: "threw",
				detail: describeError$4(error)
			};
		}
		if (idle.timedOut) return { status: "timeout" };
		const summary = extractNewAssistantText(session.deriveMessages(), entry.messageBaseline);
		const tokensHint = extractTokensHint(session.events, entry.eventBaseline);
		entry.messageBaseline = session.deriveMessages().length;
		entry.eventBaseline = session.events.length;
		return {
			status: "completed",
			summary,
			...tokensHint !== void 0 ? { tokensHint } : {}
		};
	}
	buildInitialPrompt(kind, title, bounded) {
		return [
			ANALYSIS_GUIDANCE,
			"",
			`[分析对象 / subject] kind=${kind} title=${title}`,
			"",
			`--- 会话摘要开始 / summary begin (有界输入${bounded.truncated ? ",已截断 / truncated" : ""}) ---`,
			bounded.text,
			"--- 会话摘要结束 / summary end ---"
		].join("\n");
	}
	cancelQuietly(entry) {
		try {
			entry.handle?.agent.cancel({ kind: "user" });
		} catch (error) {
			this.deps.log({
				op: "cancel",
				analysisSessionId: entry.analysisSessionId,
				found: true,
				detail: `cancel threw: ${describeError$4(error)}`
			});
		}
	}
	async disposeQuietly(entry) {
		if (entry.handle === void 0) return;
		try {
			await entry.handle.dispose();
		} catch (error) {
			this.deps.log({
				op: "cancel",
				analysisSessionId: entry.analysisSessionId,
				found: true,
				detail: `dispose threw: ${describeError$4(error)}`
			});
		}
	}
	mintSessionId() {
		return `${this.pluginName}-analysis-${this.now().toString(36)}-${++this.mintCounter}`;
	}
	failResult(phase, ident, errorCode, opts) {
		const result = {
			outcome: "failed",
			truncated: opts.truncated,
			errorCode,
			...opts.detail !== void 0 ? { detail: opts.detail } : {},
			disclaimer: ANALYSIS_DISCLAIMER
		};
		this.deps.log({
			op: "result",
			phase,
			...ident.analysisSessionId !== void 0 ? { analysisSessionId: ident.analysisSessionId } : {},
			...ident.kind !== void 0 ? { kind: ident.kind } : {},
			...ident.title !== void 0 ? { title: ident.title } : {},
			outcome: "failed",
			errorCode,
			...opts.detail !== void 0 ? { detail: opts.detail } : {},
			elapsedMs: this.now() - opts.startedAt
		});
		return result;
	}
	timeoutResult(phase, entry, truncated, startedAt, analysisSessionId) {
		const result = {
			outcome: "timeout",
			...analysisSessionId !== void 0 ? { analysisSessionId } : {},
			truncated,
			errorCode: "timeout",
			disclaimer: ANALYSIS_DISCLAIMER
		};
		this.deps.log({
			op: "result",
			phase,
			analysisSessionId: entry.analysisSessionId,
			kind: entry.kind,
			title: entry.title,
			outcome: "timeout",
			errorCode: "timeout",
			elapsedMs: this.now() - startedAt
		});
		return result;
	}
	logResult(phase, entry, result, startedAt) {
		this.deps.log({
			op: "result",
			phase,
			analysisSessionId: entry.analysisSessionId,
			kind: entry.kind,
			title: entry.title,
			outcome: result.outcome,
			...result.tokensHint !== void 0 ? { tokensHint: result.tokensHint } : {},
			truncated: result.truncated,
			elapsedMs: this.now() - startedAt
		});
	}
};
//#endregion
//#region src/config.ts
/**
* Composition config for the dsh-agent-sidecar host half (design §6, scoped
* to the task-approved field set). schemastery-validated by the cordis
* Loader; every field carries a default so a bare patch row (no `config:`
* block) mounts with zero configuration, and `.description()` strings feed
* the dsh settings pane renderer.
*
* Grouping mirrors the runtime module it feeds:
* - `daemon`   → DaemonSupervisor (src/supervisor.ts)
* - `sidecar`  → CLI/daemon invocation (command + runtime dir redirect)
* - `stream`   → Reconciler cadences (src/bridge.ts)
* - `inject`   → the guard's write gate (src/guard.ts); M2 consumes the rest
* - `analysis` / `ui` / `skill` → M3/M4 surfaces, contractual today so the
*   config face does not churn per milestone.
*
* @module
*/
const Config = z.object({
	daemon: z.object({
		policy: z.union([
			z.const("adopt-or-host"),
			z.const("adopt-only"),
			z.const("off")
		]).default("adopt-or-host").description("daemon 托管策略:adopt-or-host=探测并领养既有 daemon,否则自行拉起;adopt-only=只领养绝不拉起;off=不管理 daemon 生命周期(仍只读对账既有 daemon 的数据)"),
		backoffLimit: z.natural().min(1).default(5).description("托管失败熔断阈值:连续失败达到该次数后停止重启并进入 FAILED")
	}).description("daemon 生命周期治理"),
	sidecar: z.object({
		command: z.array(String).default(["agent-sidecar"]).description("sidecar 可执行命令(argv 前缀):PATH 名、绝对路径,或多段命令(如 [\"python3\", \"/path/to/agent-sidecar.pyz\"]);插件绝不代装"),
		runtimeDir: z.string().default("").description("运行时目录:留空用默认 ~/.agent_sidecar(尊重 AGENT_SIDECAR_RUNTIME_DIR 环境变量);非空时经环境变量传给受托管的 daemon")
	}).description("sidecar 调用方式"),
	stream: z.object({
		reconcileActiveMs: z.natural().min(100).default(2e3).description("对账快照周期(有会话工作中,毫秒)"),
		reconcileIdleMs: z.natural().min(100).default(1e4).description("对账快照周期(空闲,毫秒)")
	}).description("数据流对账节奏"),
	inject: z.object({
		enabled: z.boolean().default(false).description("注入总开关:关闭时看板隐藏全部注入入口,写接口在服务端同步拒绝(默认关闭;多用户主机不建议开启)"),
		defaultMode: z.union([z.const("queue"), z.const("steer")]).default("queue").description("注入面板默认模式:queue=排队下一轮,steer=中途注入")
	}).description("消息注入"),
	analysis: z.object({
		enabled: z.boolean().default(false).description("AI 旁路分析开关(M3;消耗模型 token,默认关闭)"),
		provider: z.string().default("").description("分析代理的 provider 路由:留空(默认)复用宿主默认模型(agentDefaultModel 服务);与 model 同时非空才生效"),
		model: z.string().default("").description("分析代理的模型 id:留空(默认)复用宿主默认模型;与 provider 同时非空才生效")
	}).description("旁路分析"),
	ui: z.object({
		timeWindowHours: z.natural().min(1).default(24).description("看板会话时间窗(小时)"),
		showDead: z.boolean().default(false).description("是否显示 dead 会话")
	}).description("看板界面"),
	skill: z.object({ provide: z.boolean().default(false).description("是否经 registerProvider 内嵌提供 agent-sidecar skill(M4 启用)") }).description("skill 模式")
});
//#endregion
//#region src/bridge.ts
/**
* Sidecar Unix-socket bridge (host half, transport layer only).
*
* Pure `node:net`; deliberately free of any cordis/dsh import so the
* protocol client stays testable in isolation and reusable outside the
* plugin context.
*
* Protocol source of truth (verified against sidecar source, not docs):
* - Requests are single-line JSON
*   `{"op":"ping"|"status"|"replay"|"subscribe"}` terminated by `\n`
*   (`sidecar/daemon.py` `_handle_client`).
* - `ping`/`status`/`replay` answer with exactly one JSON line. The official
*   client (`sidecar/client.py`) opens one fresh connection per op and
*   closes it after the response; we mirror that semantic.
* - `replay {session_id, after_seq, limit}` (T5.2) answers one bounded page
*   `{events, last_seq, truncated, count, agent, ...}` sourced from the
*   session adapter's own transcript replay (daemon `_replay_response`;
*   today only dsh sessions provide one). Unlike ping/status, {@link
*   SidecarSocketClient.replay} REJECTS with a coded
*   {@link SidecarDaemonError} instead of resolving null: the daemon error
*   vocabulary (`unknown_session` / `replay_unsupported` / `replay_failed`
*   / `invalid_request`) must reach the caller verbatim so the fusion
*   layer can degrade honestly (design §4.b.2).
* - `subscribe` answers with an ack line `{"ok":true,"op":"subscribe"}`
*   and then streams JSONL event objects until either side disconnects
*   (`sidecar/daemon.py` `_serve_subscription`). An optional
*   `{"agents":[...]}` allowlist asks the daemon to stream only those
*   agents' events (server-side filter, daemon `_parse_subscribe_agents`);
*   the ack then echoes the sorted list. The per-subscriber queue is
*   bounded (256, drop-oldest) and drops are NOT signalled on the wire
*   (`sidecar/bus.py`), which is why the stream is a trigger signal only;
*   `status` snapshots remain the source of truth (design §4.b / ADR-2).
* - Daemon-declared errors arrive as `{"ok":false,"error":{code,message}}`.
*
* @module
*/
/**
* Coded failure of a request/response op. `code` carries the daemon error
* vocabulary verbatim (`invalid_request`, `unknown_session`,
* `replay_unsupported`, `replay_failed`, ...) or one of the client-side
* transport codes: `timeout`, `connection_failed`, `connection_closed`,
* `invalid_response`.
*/
var SidecarDaemonError = class extends Error {
	code;
	constructor(code, detail) {
		super(`${code}: ${detail}`);
		this.name = "SidecarDaemonError";
		this.code = code;
	}
};
function isRecord(value) {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
function parseHttpPingInfo(value) {
	if (value === null || value === void 0) return { enabled: false };
	if (!isRecord(value) || typeof value["enabled"] !== "boolean") return null;
	if (value["enabled"] === false) return { enabled: false };
	const host = value["host"];
	const port = value["port"];
	if (typeof host !== "string" || host === "") return null;
	if (typeof port !== "number" || !Number.isInteger(port) || port < 1 || port > 65535) return null;
	return {
		enabled: true,
		host,
		port
	};
}
function parsePingInfo(value) {
	if (!isRecord(value) || value["ok"] !== true || value["op"] !== "ping") return null;
	const pid = value["pid"];
	if (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0) return null;
	const rawVersion = value["version"];
	let version;
	if (rawVersion === null || rawVersion === void 0) version = "";
	else if (typeof rawVersion === "string") version = rawVersion;
	else return null;
	const http = parseHttpPingInfo(value["http"]);
	if (http === null) return null;
	return {
		pid,
		version,
		http
	};
}
/**
* Normalize one raw status row. Rows without a usable `session_id` are
* skipped by the caller (the daemon model guarantees the field, so this
* only defends against wire corruption).
*/
function parseSessionRow(value) {
	if (!isRecord(value)) return null;
	const sessionId = value["session_id"];
	if (typeof sessionId !== "string" || sessionId === "") return null;
	const updatedAt = value["updated_at"];
	return {
		agent: typeof value["agent"] === "string" ? value["agent"] : "",
		session_id: sessionId,
		project: typeof value["project"] === "string" ? value["project"] : "",
		transcript: typeof value["transcript"] === "string" ? value["transcript"] : "",
		updated_at: typeof updatedAt === "number" && Number.isFinite(updatedAt) ? updatedAt : 0,
		title: typeof value["title"] === "string" ? value["title"] : "",
		status: typeof value["status"] === "string" ? value["status"] : "idle",
		extra: isRecord(value["extra"]) ? value["extra"] : {},
		parent_id: typeof value["parent_id"] === "string" ? value["parent_id"] : null
	};
}
function parseRecordList(value) {
	if (value === void 0) return [];
	if (!Array.isArray(value)) return null;
	const out = [];
	for (const item of value) {
		if (!isRecord(item)) return null;
		out.push(item);
	}
	return out;
}
function parseStatusSnapshot(value) {
	if (!isRecord(value) || value["ok"] !== true) return null;
	const rawSessions = value["sessions"];
	if (!Array.isArray(rawSessions)) return null;
	const sessions = [];
	for (const raw of rawSessions) {
		if (!isRecord(raw)) return null;
		const row = parseSessionRow(raw);
		if (row !== null) sessions.push(row);
	}
	const scanErrors = parseRecordList(value["scan_errors"]);
	const tailErrors = parseRecordList(value["tail_errors"]);
	if (scanErrors === null || tailErrors === null) return null;
	return {
		sessions,
		scanErrors,
		tailErrors,
		diagnostics: parseRecordList(value["diagnostics"]) ?? []
	};
}
function parseEvent(value) {
	const ts = value["ts"];
	const agent = value["agent"];
	const sessionId = value["session_id"];
	const kind = value["kind"];
	const text = value["text"];
	if (typeof ts !== "string" || typeof agent !== "string" || typeof sessionId !== "string" || typeof kind !== "string" || typeof text !== "string") return null;
	return {
		ts,
		agent,
		session_id: sessionId,
		kind,
		text,
		extra: isRecord(value["extra"]) ? value["extra"] : {}
	};
}
function daemonError(value) {
	const error = value["error"];
	if (isRecord(error)) {
		const code = String(error["code"] ?? "daemon_error");
		return new SidecarDaemonError(code, String(error["message"] ?? code));
	}
	return new SidecarDaemonError("daemon_error", String(error ?? "daemon_error"));
}
/**
* Parse one `replay` response page. Mirrors sidecar/client.py strictness:
* a non-object entry in `events` invalidates the whole response, while an
* object entry missing normalized fields is skipped defensively (the
* daemon model guarantees them).
*/
function parseReplayPage(value) {
	if (!isRecord(value) || value["ok"] !== true || value["op"] !== "replay") return null;
	const sessionId = value["session_id"];
	const agent = value["agent"];
	const rawEvents = value["events"];
	if (typeof sessionId !== "string" || typeof agent !== "string") return null;
	if (!Array.isArray(rawEvents)) return null;
	const events = [];
	for (const raw of rawEvents) {
		if (!isRecord(raw)) return null;
		const event = parseEvent(raw);
		if (event !== null) events.push(event);
	}
	const afterSeq = value["after_seq"];
	const count = value["count"];
	const lastSeq = value["last_seq"];
	return {
		sessionId,
		agent,
		afterSeq: typeof afterSeq === "number" && Number.isInteger(afterSeq) && afterSeq >= 0 ? afterSeq : 0,
		events,
		count: typeof count === "number" && Number.isInteger(count) ? count : events.length,
		lastSeq: typeof lastSeq === "number" && Number.isInteger(lastSeq) ? lastSeq : null,
		truncated: value["truncated"] === true
	};
}
/**
* Build the subscribe request line, validating an optional agents filter
* up front (before any socket exists) so misuse throws synchronously.
*/
function buildSubscribeRequest(agents) {
	if (agents === void 0) return "{\"op\":\"subscribe\"}\n";
	if (agents.length === 0 || agents.some((name) => typeof name !== "string" || name === "")) throw new RangeError("agents must be a nonempty list of nonempty agent names");
	return `${JSON.stringify({
		op: "subscribe",
		agents
	})}\n`;
}
const NEWLINE = 10;
const EMPTY = Buffer.alloc(0);
/**
* Splits a byte stream into newline-terminated lines with a hard size
* bound. An over-long line is discarded (signalled once via `onOverflow`)
* and the splitter resynchronizes at the next newline, so one oversized
* record cannot take down the whole stream or balloon memory.
*/
var LineBuffer = class {
	maxBytes;
	onLine;
	onOverflow;
	pending = EMPTY;
	dropping = false;
	constructor(maxBytes, onLine, onOverflow) {
		this.maxBytes = maxBytes;
		this.onLine = onLine;
		this.onOverflow = onOverflow;
	}
	push(chunk) {
		this.pending = this.pending.length === 0 ? chunk : Buffer.concat([this.pending, chunk]);
		for (;;) {
			const idx = this.pending.indexOf(NEWLINE);
			if (idx < 0) {
				if (this.pending.length > this.maxBytes) {
					this.pending = EMPTY;
					if (!this.dropping) {
						this.dropping = true;
						this.onOverflow();
					}
				}
				return;
			}
			const line = this.pending.subarray(0, idx);
			this.pending = this.pending.subarray(idx + 1);
			if (this.dropping) {
				this.dropping = false;
				continue;
			}
			if (line.length > this.maxBytes) {
				this.onOverflow();
				continue;
			}
			this.onLine(line);
		}
	}
};
/**
* Minimal daemon client: one fresh connection per op (matching the
* semantics of `sidecar/client.py`), single-line JSON requests, JSONL
* responses, bounded reads. All request/response failures resolve to
* `null` instead of throwing — the caller (Reconciler/Supervisor) owns
* the health policy.
*/
var SidecarSocketClient = class {
	socketPath;
	timeoutMs;
	replayTimeoutMs;
	maxLineBytes;
	constructor(opts) {
		this.socketPath = opts.socketPath;
		this.timeoutMs = opts.timeoutMs ?? 1e3;
		this.replayTimeoutMs = opts.replayTimeoutMs ?? 15e3;
		this.maxLineBytes = opts.maxLineBytes ?? 33554432;
		if (this.timeoutMs <= 0 || this.replayTimeoutMs <= 0 || this.maxLineBytes <= 0) throw new RangeError("client bounds are invalid");
	}
	/** `ping` op; `null` on refusal, timeout, or an invalid/error response. */
	async ping() {
		return parsePingInfo(await this.requestLine("ping"));
	}
	/** `status` op; `null` on refusal, timeout, or an invalid/error response. */
	async status() {
		return parseStatusSnapshot(await this.requestLine("status"));
	}
	/**
	* `replay` op (T5.2): one bounded page of normalized historical events
	* after `afterSeq`. Unlike ping/status this REJECTS with a coded
	* {@link SidecarDaemonError} — daemon codes pass through verbatim and
	* transport failures get client codes — because the caller (FusionQuery
	* seam) distinguishes degradation reasons instead of polling health.
	* Local misuse throws a RangeError (mirrors sidecar/client.py's
	* ValueError). `limit` is forwarded as-is; the daemon enforces its own
	* 1..1024 bound and answers `invalid_request` beyond it.
	*/
	async replay(sessionId, afterSeq = 0, limit) {
		if (typeof sessionId !== "string" || sessionId === "") throw new RangeError("sessionId must be a nonempty string");
		if (!Number.isInteger(afterSeq) || afterSeq < 0) throw new RangeError("afterSeq must be a nonnegative integer");
		if (limit !== void 0 && (!Number.isInteger(limit) || limit <= 0)) throw new RangeError("limit must be a positive integer");
		const payload = {
			op: "replay",
			session_id: sessionId,
			after_seq: afterSeq
		};
		if (limit !== void 0) payload["limit"] = limit;
		const value = await this.requestObject(payload, this.replayTimeoutMs);
		if (isRecord(value) && value["ok"] === false) throw daemonError(value);
		const page = parseReplayPage(value);
		if (page === null) throw new SidecarDaemonError("invalid_response", "daemon replay response has no valid events list");
		return page;
	}
	/**
	* Open a subscribe stream: write the op, validate the ack, then deliver
	* each JSONL event through `handlers.onEvent`. After the ack the
	* connection may idle indefinitely (no timeout), matching
	* sidecar/client.py which disables its socket timeout post-handshake.
	* An `opts.agents` allowlist becomes the daemon-side stream filter.
	*/
	subscribe(handlers, opts = {}) {
		const request = buildSubscribeRequest(opts.agents);
		let closed = false;
		let ready = false;
		const socket = createConnection({ path: this.socketPath });
		const finish = (err) => {
			if (closed) return;
			closed = true;
			socket.destroy();
			queueMicrotask(() => handlers.onClose?.(err));
		};
		const lines = new LineBuffer(this.maxLineBytes, (line) => {
			if (closed) return;
			let value;
			try {
				value = JSON.parse(line.toString("utf8"));
			} catch {
				if (!ready) {
					finish(/* @__PURE__ */ new Error("daemon returned an invalid subscribe acknowledgement"));
					return;
				}
				handlers.onDrop?.("invalid_json");
				return;
			}
			if (!ready) {
				if (isRecord(value) && value["ok"] === true && value["op"] === "subscribe" && !("error" in value)) {
					ready = true;
					socket.setTimeout(0);
					handlers.onReady?.();
				} else if (isRecord(value) && value["ok"] === false) finish(daemonError(value));
				else finish(/* @__PURE__ */ new Error("daemon returned an invalid subscribe acknowledgement"));
				return;
			}
			if (!isRecord(value)) {
				handlers.onDrop?.("invalid_event");
				return;
			}
			if (value["ok"] === false) {
				finish(daemonError(value));
				return;
			}
			const event = parseEvent(value);
			if (event === null) {
				handlers.onDrop?.("invalid_event");
				return;
			}
			handlers.onEvent(event);
		}, () => {
			if (!closed) handlers.onDrop?.("line_too_long");
		});
		socket.setTimeout(this.timeoutMs);
		socket.once("timeout", () => {
			if (!ready) finish(/* @__PURE__ */ new Error("subscribe handshake timed out"));
		});
		socket.once("error", (err) => finish(err));
		socket.once("close", () => finish());
		socket.once("connect", () => {
			socket.write(request);
		});
		socket.on("data", (chunk) => lines.push(chunk));
		return { close: () => finish() };
	}
	/**
	* Send one single-line JSON request and read one JSONL response line,
	* REJECTING with a coded {@link SidecarDaemonError} on every transport
	* failure (the replay path needs error provenance, not just null).
	*/
	requestObject(payload, timeoutMs) {
		return new Promise((resolve, reject) => {
			let settled = false;
			const socket = createConnection({ path: this.socketPath });
			const finish = (settle) => {
				if (settled) return;
				settled = true;
				socket.destroy();
				settle();
			};
			const fail = (code, detail) => {
				finish(() => reject(new SidecarDaemonError(code, detail)));
			};
			const lines = new LineBuffer(this.maxLineBytes, (line) => {
				let value;
				try {
					value = JSON.parse(line.toString("utf8"));
				} catch {
					fail("invalid_response", "daemon returned an unparsable response line");
					return;
				}
				finish(() => resolve(value));
			}, () => fail("invalid_response", "daemon response line exceeded the size bound"));
			socket.setTimeout(timeoutMs);
			socket.once("timeout", () => fail("timeout", "daemon did not answer within the bound"));
			socket.once("error", (err) => fail("connection_failed", err.message));
			socket.once("close", () => fail("connection_closed", "connection closed before a response line"));
			socket.once("connect", () => {
				socket.write(`${JSON.stringify(payload)}\n`);
			});
			socket.on("data", (chunk) => lines.push(chunk));
		});
	}
	/** Send one single-line JSON request and read one JSONL response line. */
	requestLine(op) {
		return new Promise((resolve) => {
			let settled = false;
			const socket = createConnection({ path: this.socketPath });
			const finish = (value) => {
				if (settled) return;
				settled = true;
				socket.destroy();
				resolve(value);
			};
			const lines = new LineBuffer(this.maxLineBytes, (line) => {
				let value;
				try {
					value = JSON.parse(line.toString("utf8"));
				} catch {
					value = null;
				}
				finish(value);
			}, () => finish(null));
			socket.setTimeout(this.timeoutMs);
			socket.once("timeout", () => finish(null));
			socket.once("error", () => finish(null));
			socket.once("close", () => finish(null));
			socket.once("connect", () => {
				socket.write(JSON.stringify({ op }) + "\n");
			});
			socket.on("data", (chunk) => lines.push(chunk));
		});
	}
};
/**
* Dual-cadence status reconciliation plus subscribe-stream supervision:
* - `status` snapshots run on an active (any working session) or idle
*   cadence and are applied as the authoritative full state.
* - each subscribe event is folded into the store as a hint and schedules
*   one debounced early reconcile.
* - a FAILED snapshot (daemon absent or not yet ready) retries on a short
*   backoff (250ms doubling, capped at the current cadence) instead of
*   sleeping a whole cadence period — a cold start where the very first
*   `status` races the daemon socket must not cost a full `idleMs`
*   (M1 acceptance ②). A success resets the streak to the steady cadence.
* - `reconcileNow()` is public so the supervisor can hand off "daemon just
*   became reachable" (ADOPTED/HOSTED are ping-gated) as one immediate
*   reconcile.
* - a dropped stream marks `streamHealth=degraded` and reconnects with
*   bounded exponential backoff (1s doubling to a 30s cap, retrying
*   forever); a validated ack restores `streamHealth=ok` and resets the
*   backoff.
*/
var Reconciler = class {
	client;
	store;
	activeMs;
	idleMs;
	debounceMs;
	reconnectMinMs;
	reconnectMaxMs;
	failureBackoffMs;
	running = false;
	backoffMs;
	/** Consecutive failed reconciles; drives the short retry backoff. */
	failStreak = 0;
	pollTimer = null;
	kickTimer = null;
	reconnectTimer = null;
	subscription = null;
	reconcileInFlight = false;
	reconcileQueued = false;
	constructor(client, store, opts = {}) {
		this.client = client;
		this.store = store;
		this.activeMs = opts.activeMs ?? 2e3;
		this.idleMs = opts.idleMs ?? 1e4;
		this.debounceMs = opts.debounceMs ?? 200;
		this.reconnectMinMs = opts.reconnectMinMs ?? 1e3;
		this.reconnectMaxMs = opts.reconnectMaxMs ?? 3e4;
		this.failureBackoffMs = opts.failureBackoffMs ?? 250;
		this.backoffMs = this.reconnectMinMs;
	}
	start() {
		if (this.running) return;
		this.running = true;
		this.backoffMs = this.reconnectMinMs;
		this.failStreak = 0;
		this.openSubscription();
		this.reconcileNow();
	}
	stop() {
		if (!this.running) return;
		this.running = false;
		if (this.pollTimer !== null) clearTimeout(this.pollTimer);
		if (this.kickTimer !== null) clearTimeout(this.kickTimer);
		if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
		this.pollTimer = null;
		this.kickTimer = null;
		this.reconnectTimer = null;
		const subscription = this.subscription;
		this.subscription = null;
		subscription?.close();
	}
	/**
	* Run one immediate `status` reconcile and reschedule the next poll from
	* its outcome. Public as the supervisor hand-off seam: the plugin entry
	* calls this on the ADOPTED/HOSTED transition (both are gated on a
	* successful ping, so the socket is known-reachable at that moment).
	* Coalesces with an in-flight reconcile; a no-op when stopped.
	*/
	async reconcileNow() {
		if (!this.running) return;
		if (this.reconcileInFlight) {
			this.reconcileQueued = true;
			return;
		}
		this.reconcileInFlight = true;
		try {
			const snapshot = await this.client.status();
			if (snapshot === null) this.failStreak += 1;
			else {
				this.failStreak = 0;
				if (this.running) this.store.applySnapshot(snapshot.sessions);
			}
		} finally {
			this.reconcileInFlight = false;
		}
		if (!this.running) return;
		if (this.reconcileQueued) {
			this.reconcileQueued = false;
			this.reconcileNow();
			return;
		}
		this.scheduleNext();
	}
	scheduleNext() {
		if (this.pollTimer !== null) clearTimeout(this.pollTimer);
		const cadence = this.store.hasWorkingSessions() ? this.activeMs : this.idleMs;
		const delay = this.failStreak > 0 ? Math.min(this.failureBackoffMs * 2 ** (this.failStreak - 1), cadence) : cadence;
		this.pollTimer = setTimeout(() => {
			this.pollTimer = null;
			this.reconcileNow();
		}, delay);
	}
	/** Schedule one debounced early reconcile (subscribe events are hints). */
	kick() {
		if (!this.running || this.kickTimer !== null) return;
		this.kickTimer = setTimeout(() => {
			this.kickTimer = null;
			this.reconcileNow();
		}, this.debounceMs);
	}
	openSubscription() {
		if (!this.running) return;
		this.subscription = this.client.subscribe({
			onReady: () => {
				if (!this.running) return;
				this.backoffMs = this.reconnectMinMs;
				this.store.setStreamHealth("ok");
			},
			onEvent: (ev) => {
				if (!this.running) return;
				this.store.applyEvent(ev);
				this.kick();
			},
			onDrop: () => {
				this.kick();
			},
			onClose: () => {
				this.subscription = null;
				if (!this.running) return;
				this.store.setStreamHealth("degraded");
				const delay = this.backoffMs;
				this.backoffMs = Math.min(this.backoffMs * 2, this.reconnectMaxMs);
				this.reconnectTimer = setTimeout(() => {
					this.reconnectTimer = null;
					this.openSubscription();
				}, delay);
			}
		});
	}
};
/** Sha256 hex prefix length recorded in logs (matches inject-gateway). */
const SHA_LOG_CHARS$1 = 12;
function describeError$3(error) {
	return error instanceof Error ? error.message : String(error);
}
/** Race a resume against the bounded wait; null = timed out (still pending). */
async function resumeWithin(resume, timeoutMs) {
	let timer;
	try {
		return await Promise.race([resume, new Promise((resolve) => {
			timer = setTimeout(() => resolve(null), timeoutMs);
		})]);
	} finally {
		if (timer !== void 0) clearTimeout(timer);
	}
}
function createDshInjectExecutor(deps) {
	const pluginName = deps.pluginName ?? "agent-sidecar";
	const log = deps.log ?? (() => {});
	const resumeTimeoutMs = deps.resumeTimeoutMs ?? 3e4;
	return {
		kind: "dsh",
		async execute(req) {
			const sessionId = req.target.sessionId;
			const messageBytes = Buffer.byteLength(req.message, "utf8");
			const messageSha12 = createHash("sha256").update(req.message, "utf8").digest("hex").slice(0, SHA_LOG_CHARS$1);
			const baseMeta = {
				requestId: req.requestId,
				sessionId,
				mode: req.mode,
				messageBytes,
				messageSha12
			};
			let agent = deps.agents.get(sessionId);
			const resumed = agent === void 0;
			if (agent === void 0) {
				log("debug", "dsh session not loaded; resuming", baseMeta);
				let handle;
				try {
					handle = await resumeWithin(deps.agents.resume({ resumeSessionId: sessionId }), resumeTimeoutMs);
				} catch (error) {
					const detail = describeError$3(error);
					log("warn", "dsh resume failed", {
						...baseMeta,
						error: detail
					});
					return {
						outcome: "failed",
						errorCode: "session_not_found",
						detail
					};
				}
				if (handle === null) {
					log("warn", "dsh resume timed out", {
						...baseMeta,
						resumeTimeoutMs
					});
					return {
						outcome: "failed",
						errorCode: "timeout",
						detail: `resume did not settle within ${resumeTimeoutMs}ms`
					};
				}
				agent = handle.agent;
			}
			const message = {
				id: `${pluginName}-${req.requestId}`,
				role: "user",
				content: [{
					type: "text",
					text: req.message
				}],
				source: {
					kind: "plugin",
					plugin: pluginName
				}
			};
			try {
				if (req.mode === "steer") agent.steer(message);
				else agent.followup(message);
			} catch (error) {
				const detail = describeError$3(error);
				log("warn", "dsh injection call threw", {
					...baseMeta,
					resumed,
					error: detail
				});
				return {
					outcome: "failed",
					errorCode: "executor_error",
					detail
				};
			}
			log("info", "dsh injection delivered", {
				...baseMeta,
				resumed
			});
			return { outcome: "delivered" };
		}
	};
}
const DSH_AGENT = "dsh";
const KEY_SEP = "\0";
function integerOrNull(value) {
	return typeof value === "number" && Number.isInteger(value) ? value : null;
}
function secondsToMs(seconds) {
	return typeof seconds === "number" && Number.isFinite(seconds) ? Math.round(seconds * 1e3) : 0;
}
function parseTs(ts) {
	const ms = Date.parse(ts);
	return Number.isFinite(ms) ? ms : 0;
}
/** Latest-wins `session/title` payload fold (`{title: string}`). */
function extractTitle(data) {
	if (typeof data !== "object" || data === null || Array.isArray(data)) return null;
	const title = data["title"];
	return typeof title === "string" && title !== "" ? title : null;
}
/** Correlation-key normalization: strip trailing slashes (keep root `/`). */
function normalizeProject(project) {
	if (project.length > 1 && project.endsWith("/")) {
		const stripped = project.replace(/\/+$/, "");
		return stripped === "" ? "/" : stripped;
	}
	return project;
}
function describeError$2(error) {
	return error instanceof Error ? error.message : String(error);
}
/**
* Dedup identity of one sidecar event within a single session's timeline.
* The dsh adapter legally normalizes ONE dsh record into SEVERAL events
* sharing the same `extra.seq` (reasoning+text blocks of an assistant
* message, multi-block user messages, spliced inbox inserts —
* sidecar/adapters/dsh.py content_block_events), and no per-block ordinal
* exists on the wire — so seq alone would silently drop sibling events.
* The identity is therefore `seq+kind+text`: the same underlying event
* seen through both replay and the ring still collapses (identical
* normalized kind/text), while same-seq siblings stay distinct. Must stay
* in sync with the client mirror (client/detail/logic.ts `entryKey`).
*/
function sidecarEventKey(ev) {
	const seq = integerOrNull(ev.extra?.["seq"]);
	return seq !== null ? `s:${seq}${KEY_SEP}${ev.kind}${KEY_SEP}${ev.text}` : `t:${ev.ts}${KEY_SEP}${ev.kind}${KEY_SEP}${ev.text}`;
}
/** Strictly-older-than-cursor predicate over the merged ascending order. */
function isBeforeCursor(entry, cursor) {
	if (cursor.seq !== null && entry.seq !== null) return entry.seq < cursor.seq;
	return entry.ts < cursor.ts;
}
/** One seq-carrying sidecar event as its own timeline entry. */
function sidecarSeqEntry(seq, ev) {
	return {
		origin: "sidecar",
		seq,
		ts: parseTs(ev.ts),
		kind: ev.kind,
		text: ev.text,
		data: void 0,
		extra: ev.extra ?? null
	};
}
/**
* Merge dsh events (authoritative seq domain) with sidecar events.
* One dsh record can normalize into several sidecar events sharing the
* same `extra.seq` (multi-block messages), so twins are grouped per seq:
* the FIRST twin folds into the matching dsh entry (normalized text +
* extra supplement, dsh primary) and every further sibling stays its own
* entry — dropping siblings would silently lose blocks (F1). Seq-carrying
* entries keep exact seq order (same-seq groups keep dsh-then-block
* arrival order via the stable sort); seq-less entries interleave by
* timestamp.
*/
function mergeTimeline(dshEvents, sidecarEvents) {
	const twinsBySeq = /* @__PURE__ */ new Map();
	const unseqed = [];
	for (const ev of sidecarEvents) {
		const seq = integerOrNull(ev.extra?.["seq"]);
		if (seq !== null) {
			const group = twinsBySeq.get(seq);
			if (group === void 0) twinsBySeq.set(seq, [ev]);
			else group.push(ev);
		} else unseqed.push({
			origin: "sidecar",
			seq: null,
			ts: parseTs(ev.ts),
			kind: ev.kind,
			text: ev.text,
			data: void 0,
			extra: ev.extra ?? null
		});
	}
	const seqDomain = [];
	const dshSeqs = /* @__PURE__ */ new Set();
	for (const ev of dshEvents) {
		const twins = dshSeqs.has(ev.seq) ? void 0 : twinsBySeq.get(ev.seq);
		dshSeqs.add(ev.seq);
		const first = twins?.[0];
		seqDomain.push({
			origin: "dsh",
			seq: ev.seq,
			ts: ev.time,
			kind: ev.type,
			text: first?.text ?? "",
			data: ev.data,
			extra: first?.extra ?? null
		});
		if (twins !== void 0) for (let i = 1; i < twins.length; i += 1) {
			const sibling = twins[i];
			if (sibling !== void 0) seqDomain.push(sidecarSeqEntry(ev.seq, sibling));
		}
	}
	for (const [seq, twins] of twinsBySeq) {
		if (dshSeqs.has(seq)) continue;
		for (const ev of twins) seqDomain.push(sidecarSeqEntry(seq, ev));
	}
	seqDomain.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
	unseqed.sort((a, b) => a.ts - b.ts);
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
/**
* The fused query surface. Lifecycle: `start()` subscribes to the dsh
* feed, `stop()` disposes subscriptions and drops all cached state; the
* wiring feeds the sidecar subscribe stream through
* {@link ingestSidecarEvent}. All query methods are on-demand pulls.
*/
var FusionQuery = class {
	store;
	dshEvents;
	getSessionQueryThunk;
	replaySource;
	now;
	maxEventsPerSession;
	maxSessions;
	/** Live in-process dsh sessions keyed by session id. */
	live = /* @__PURE__ */ new Map();
	/** Bounded per-session sidecar event rings; insertion order = feed recency. */
	buffers = /* @__PURE__ */ new Map();
	disposers = [];
	started = false;
	constructor(opts) {
		this.store = opts.store;
		this.dshEvents = opts.dshEvents ?? null;
		this.getSessionQueryThunk = opts.getSessionQuery ?? null;
		this.replaySource = opts.replay ?? null;
		this.now = opts.now ?? Date.now;
		this.maxEventsPerSession = opts.maxBufferedEventsPerSession ?? 200;
		this.maxSessions = opts.maxBufferedSessions ?? 256;
		if (this.maxEventsPerSession <= 0 || this.maxSessions <= 0) throw new RangeError("fusion buffer bounds are invalid");
	}
	/** Subscribe to the in-process feed (idempotent). */
	start() {
		if (this.started) return;
		this.started = true;
		if (this.dshEvents === null) return;
		this.disposers.push(this.dshEvents.on("session/created", (session) => {
			this.ensureLive(session);
		}), this.dshEvents.on("session/event", (session, ev) => {
			this.handleDshEvent(session, ev);
		}), this.dshEvents.on("session/disposed", (session) => {
			this.live.delete(session.id);
		}));
	}
	/** Dispose subscriptions and drop all cached state (idempotent). */
	stop() {
		if (!this.started) return;
		this.started = false;
		const disposers = this.disposers;
		this.disposers = [];
		for (const dispose of disposers) dispose();
		this.live.clear();
		this.buffers.clear();
	}
	/**
	* Feed one sidecar subscribe-stream event into the bounded ring
	* (timeline hints only; the stream stays a trigger signal, ADR-2).
	*/
	ingestSidecarEvent(ev) {
		if (typeof ev.session_id !== "string" || ev.session_id === "") return;
		let ring = this.buffers.get(ev.session_id);
		if (ring === void 0) ring = [];
		else this.buffers.delete(ev.session_id);
		ring.push(ev);
		if (ring.length > this.maxEventsPerSession) ring.splice(0, ring.length - this.maxEventsPerSession);
		this.buffers.set(ev.session_id, ring);
		while (this.buffers.size > this.maxSessions) {
			const oldest = this.buffers.keys().next();
			if (oldest.done === true) break;
			this.buffers.delete(oldest.value);
		}
	}
	/**
	* Deduplicated cross-agent session list, most recently active first.
	* dsh sessions live in this process win over their sidecar rows
	* (which then only supplement); cold dsh sessions and non-dsh agents
	* come from the sidecar alone.
	*/
	getUnifiedSessions() {
		const board = this.store.getBoardState();
		const out = /* @__PURE__ */ new Map();
		const mergedIds = /* @__PURE__ */ new Set();
		for (const row of board.sessions) {
			const liveEntry = row.agent === DSH_AGENT ? this.live.get(row.session_id) : void 0;
			if (liveEntry !== void 0) {
				mergedIds.add(row.session_id);
				out.set(`${row.agent}${KEY_SEP}${row.session_id}`, this.mergeRow(liveEntry, row));
			} else out.set(`${row.agent}${KEY_SEP}${row.session_id}`, fromSidecarRow(row));
		}
		for (const [id, entry] of this.live) {
			if (mergedIds.has(id)) continue;
			out.set(`${DSH_AGENT}${KEY_SEP}${id}`, fromDshLive(entry));
		}
		const sessions = [...out.values()];
		sessions.sort((a, b) => b.lastActivityAt - a.lastActivityAt || a.sessionId.localeCompare(b.sessionId));
		return sessions;
	}
	/**
	* Cross-agent project correlation groups within a time window
	* (project path + window is the correlation key, design §4.e.2).
	*/
	getProjectGroups(opts = {}) {
		const windowMs = opts.windowMs ?? 864e5;
		const cutoff = (opts.now ?? this.now()) - windowMs;
		const groups = /* @__PURE__ */ new Map();
		for (const session of this.getUnifiedSessions()) {
			if (session.lastActivityAt < cutoff) continue;
			const project = normalizeProject(session.project);
			let group = groups.get(project);
			if (group === void 0) {
				group = {
					project,
					agents: [],
					sessions: [],
					lastActivityAt: 0
				};
				groups.set(project, group);
			}
			group.sessions.push(session);
			if (!group.agents.includes(session.agent)) group.agents.push(session.agent);
			if (session.lastActivityAt > group.lastActivityAt) group.lastActivityAt = session.lastActivityAt;
		}
		const out = [...groups.values()];
		for (const group of out) group.agents.sort();
		out.sort((a, b) => b.lastActivityAt - a.lastActivityAt || a.project.localeCompare(b.project));
		return out;
	}
	/**
	* One merged timeline page for a session, ascending, deduplicated by
	* event identity (seq+kind+text for seq-carrying events — same-seq
	* sibling events from multi-block records all survive), newest window
	* first with a backward cursor. Sources are pulled on demand; a
	* missing/failing source silently narrows the page (provenance is
	* reported in `sources`).
	*/
	async getSessionTimeline(sessionId, opts = {}) {
		const limit = Math.max(1, Math.floor(opts.limit ?? 100));
		const sources = {
			dshLive: false,
			dshCold: false,
			sidecarReplay: false,
			sidecarBuffer: false
		};
		let dshEvents = [];
		const liveEntry = this.live.get(sessionId);
		if (liveEntry !== void 0) {
			dshEvents = liveEntry.session.events;
			sources.dshLive = true;
		} else {
			const engine = this.resolveSessionQuery();
			if (engine !== null) try {
				dshEvents = (await engine.readSession(sessionId)).events;
				sources.dshCold = true;
			} catch {}
		}
		const sidecarEvents = [];
		const seen = /* @__PURE__ */ new Set();
		const addSidecar = (ev) => {
			const key = sidecarEventKey(ev);
			if (seen.has(key)) return false;
			seen.add(key);
			sidecarEvents.push(ev);
			return true;
		};
		if (this.replaySource !== null) try {
			const replayed = await this.replaySource.replay({ sessionId });
			for (const ev of replayed) addSidecar(ev);
			sources.sidecarReplay = true;
		} catch {}
		const ring = this.buffers.get(sessionId);
		if (ring !== void 0 && ring.length > 0) {
			sources.sidecarBuffer = true;
			for (const ev of ring) addSidecar(ev);
		}
		const entries = mergeTimeline(dshEvents, sidecarEvents);
		let endIdx = entries.length;
		const before = opts.before ?? null;
		if (before !== null) {
			endIdx = 0;
			while (endIdx < entries.length) {
				const entry = entries[endIdx];
				if (entry === void 0 || !isBeforeCursor(entry, before)) break;
				endIdx += 1;
			}
		}
		let startIdx = Math.max(0, endIdx - limit);
		const boundary = entries[startIdx];
		if (boundary !== void 0 && boundary.seq !== null) while (startIdx > 0 && entries[startIdx - 1]?.seq === boundary.seq) startIdx -= 1;
		const window = entries.slice(startIdx, endIdx);
		const first = window[0];
		return {
			sessionId,
			entries: window,
			cursor: startIdx > 0 && first !== void 0 ? {
				seq: first.seq,
				ts: first.ts
			} : null,
			sources
		};
	}
	/**
	* dsh lineage via `sessionQuery.traceSession`; degrades to
	* `trace: null` + reason when the service is absent or the trace
	* fails (never throws).
	*/
	async getLineage(sessionId) {
		const engine = this.resolveSessionQuery();
		if (engine === null) return {
			available: false,
			trace: null,
			reason: "session_query_unavailable"
		};
		try {
			return {
				available: true,
				trace: await engine.traceSession(sessionId),
				reason: null
			};
		} catch (error) {
			return {
				available: false,
				trace: null,
				reason: "trace_failed",
				detail: describeError$2(error)
			};
		}
	}
	/**
	* Cross-agent search. With sessionQuery mounted, dsh sessions get
	* full-text ranking (hits first, engine order); without it — or when
	* the engine call fails — the deep query degrades to title/project
	* substring filtering over the unified view (`filter-only`), without
	* error. Non-dsh agents always use the filter path (the sidecar has
	* no search API).
	*/
	async searchSessions(query, opts = {}) {
		const limit = Math.max(1, Math.floor(opts.limit ?? 50));
		const needle = query.trim().toLowerCase();
		const engine = this.resolveSessionQuery();
		let mode = engine !== null ? "full-text" : "filter-only";
		if (needle === "") return {
			mode,
			items: []
		};
		const unified = this.getUnifiedSessions();
		const items = [];
		const seen = /* @__PURE__ */ new Set();
		if (engine !== null) try {
			const page = await engine.searchSessions({
				query,
				limit
			});
			const dshById = /* @__PURE__ */ new Map();
			for (const session of unified) if (session.agent === DSH_AGENT) dshById.set(session.sessionId, session);
			for (const hit of page.items) {
				const session = dshById.get(hit.header.id);
				if (session === void 0) continue;
				const key = `${session.agent}${KEY_SEP}${session.sessionId}`;
				if (seen.has(key)) continue;
				seen.add(key);
				items.push({
					session,
					matchedBy: "full-text",
					snippet: hit.bestMatch.snippet
				});
			}
		} catch {
			mode = "filter-only";
		}
		for (const session of unified) {
			const key = `${session.agent}${KEY_SEP}${session.sessionId}`;
			if (seen.has(key)) continue;
			if (session.title.toLowerCase().includes(needle)) {
				seen.add(key);
				items.push({
					session,
					matchedBy: "title",
					snippet: null
				});
			} else if (session.project.toLowerCase().includes(needle)) {
				seen.add(key);
				items.push({
					session,
					matchedBy: "project",
					snippet: null
				});
			}
		}
		return {
			mode,
			items: items.slice(0, limit)
		};
	}
	/** Current capability face (sessionQuery re-resolved on every call). */
	getCapabilities() {
		const engineAvailable = this.resolveSessionQuery() !== null;
		return {
			dshEvents: {
				available: this.dshEvents !== null,
				liveSessions: this.live.size
			},
			sessionQuery: {
				available: engineAvailable,
				reason: engineAvailable ? null : "session_query_unavailable"
			},
			search: { mode: engineAvailable ? "full-text" : "filter-only" }
		};
	}
	resolveSessionQuery() {
		if (this.getSessionQueryThunk === null) return null;
		try {
			return this.getSessionQueryThunk() ?? null;
		} catch {
			return null;
		}
	}
	/**
	* Register a live session (first `session/created` or, when the feed
	* attached late, first `session/event`), folding title/seq facts from
	* the existing log tail without copying it.
	*/
	ensureLive(session) {
		let entry = this.live.get(session.id);
		if (entry !== void 0) return entry;
		const events = session.events;
		const tail = events.length > 0 ? events[events.length - 1] : void 0;
		let title = null;
		for (let i = events.length - 1; i >= 0; i -= 1) {
			const ev = events[i];
			if (ev !== void 0 && ev.type === "session/title") {
				const candidate = extractTitle(ev.data);
				if (candidate !== null) {
					title = candidate;
					break;
				}
			}
		}
		entry = {
			session,
			title,
			lastSeq: tail !== void 0 ? tail.seq : null,
			lastEventAt: tail !== void 0 ? tail.time : null
		};
		this.live.set(session.id, entry);
		return entry;
	}
	handleDshEvent(session, ev) {
		const entry = this.ensureLive(session);
		if (entry.lastSeq === null || ev.seq > entry.lastSeq) entry.lastSeq = ev.seq;
		if (entry.lastEventAt === null || ev.time > entry.lastEventAt) entry.lastEventAt = ev.time;
		if (ev.type === "session/title") {
			const title = extractTitle(ev.data);
			if (title !== null) entry.title = title;
		}
	}
	/** Merge one live dsh entry with its sidecar row (dsh primary). */
	mergeRow(liveEntry, row) {
		const header = liveEntry.session.header;
		const dshActivityMs = liveEntry.lastEventAt ?? header.createdAt;
		return {
			agent: DSH_AGENT,
			sessionId: liveEntry.session.id,
			origin: "merged",
			live: true,
			status: row.status,
			title: liveEntry.title ?? row.title,
			project: header.cwd ?? row.project,
			lastActivityAt: Math.max(dshActivityMs, secondsToMs(row.updated_at)),
			lastEvent: row.last_event ?? null,
			lastSeq: liveEntry.lastSeq ?? integerOrNull(row.extra?.["seq"]),
			gap: row.gap === true,
			parentId: header.parentSession ?? (typeof row.parent_id === "string" ? row.parent_id : null),
			extra: row.extra ?? {}
		};
	}
};
/** Cold fallback / non-dsh row: sidecar is the only source. */
function fromSidecarRow(row) {
	return {
		agent: row.agent,
		sessionId: row.session_id,
		origin: "sidecar",
		live: false,
		status: row.status,
		title: row.title,
		project: row.project,
		lastActivityAt: secondsToMs(row.updated_at),
		lastEvent: row.last_event ?? null,
		lastSeq: integerOrNull(row.extra?.["seq"]),
		gap: row.gap === true,
		parentId: typeof row.parent_id === "string" ? row.parent_id : null,
		extra: row.extra ?? {}
	};
}
/** Live dsh session the sidecar has not (yet) observed on disk. */
function fromDshLive(entry) {
	const header = entry.session.header;
	return {
		agent: DSH_AGENT,
		sessionId: entry.session.id,
		origin: "dsh-live",
		live: true,
		status: "unknown",
		title: entry.title ?? "",
		project: header.cwd ?? "",
		lastActivityAt: entry.lastEventAt ?? header.createdAt,
		lastEvent: null,
		lastSeq: entry.lastSeq,
		gap: false,
		parentId: header.parentSession ?? null,
		extra: {}
	};
}
//#endregion
//#region src/inject-gateway.ts
/**
* InjectGateway — the single entry point of the injection write path
* (design §4.d dual-path injection, §4.f.5 / §5.3 two-phase confirm,
* §8 threat model: server-issued one-time confirmToken).
*
* Unifies the three planes both injection paths must share (ADR-4):
*
* - **Confirmation**: two-phase `prepare` → `execute`. `prepare` re-verifies
*   the target live and issues a crypto-random one-time confirmToken
*   (≥128 bit, 60s TTL) bound to requestId + target + mode + message sha256.
*   `execute` refuses missing / expired / reused tokens, and refuses a
*   message whose hash differs from the prepare-time binding (anti-swap).
*   Any execute attempt against a live token voids it, success or not
*   (consume-on-attempt).
* - **Idempotency**: the first execute result per requestId is cached
*   (5 min TTL) and replayed on repeats. `outcome: 'unknown'` is terminal:
*   a repeated execute returns the cached unknown and NEVER re-fires the
*   executor (S6 — no retry through the gateway).
* - **Logging**: exactly one entry per prepare/execute carrying ts /
*   requestId / target / mode / phase / result / errorCode / message byte
*   size and sha256 prefix — never the message body or head plaintext
*   (the head preview only travels in the prepare response for the UI).
*
* The token gate defends against browser-mediated attackers only; it does
* not claim to stop a local process that can drive both phases itself
* (ADR-8 trust posture — same as guard.ts).
*
* Pure DI: no cordis/dsh imports. Path executors (dsh in-process,
* sidecar send CLI) are injected; this module owns the contract, not the
* transport.
*
* @module
*/
/** Message byte cap, aligned with the sidecar send 16 KiB limit (§4.d). */
const MAX_MESSAGE_BYTES = 16 * 1024;
/** confirmToken lifetime (§4.f.5). */
const TOKEN_TTL_MS = 6e4;
/** Idempotency window: how long a first execute result is replayable. */
const RESULT_CACHE_TTL_MS = 5 * 6e4;
/** 16 random bytes = 128 bits, the spec floor for the confirmToken. */
const TOKEN_BYTES = 16;
/** Head preview cap (chars) for the confirm dialog; UI-only, never logged. */
const HEAD_PREVIEW_CHARS = 120;
/** Bound of the internal audit ring served by {@link InjectGateway.getRecentLog}. */
const LOG_RING_LIMIT = 256;
const DEFAULT_LOG_QUERY_LIMIT = 50;
/** Sha256 hex prefix length recorded in logs. */
const SHA_LOG_CHARS = 12;
/** External agents reachable through the sidecar `send` CLI path (§4.d). */
const SEND_CLI_AGENTS = /* @__PURE__ */ new Set([
	"claude",
	"codex",
	"cursor-cli"
]);
function digestMessage(message) {
	const sha256 = createHash("sha256").update(message, "utf8").digest("hex");
	return {
		bytes: Buffer.byteLength(message, "utf8"),
		sha256,
		sha12: sha256.slice(0, SHA_LOG_CHARS)
	};
}
/** Returns a rejection detail, or null when the message is acceptable. */
function validateMessage(message, bytes) {
	if (bytes === 0) return "message is empty";
	if (message.includes("\0")) return "message contains a NUL byte";
	if (bytes > 16384) return `message is ${bytes} bytes; limit is ${MAX_MESSAGE_BYTES}`;
	return null;
}
/** Constant-time token comparison (length leak is inherent and harmless). */
function tokenEquals(expected, provided) {
	const a = Buffer.from(expected, "utf8");
	const b = Buffer.from(provided, "utf8");
	return a.length === b.length && timingSafeEqual(a, b);
}
/** The confirmation / idempotency / logging hub for both injection paths. */
var InjectGateway = class {
	deps;
	now;
	randomId;
	pending = /* @__PURE__ */ new Map();
	results = /* @__PURE__ */ new Map();
	logRing = [];
	constructor(deps) {
		this.deps = deps;
		this.now = deps.now ?? Date.now;
		this.randomId = deps.randomId ?? randomUUID;
	}
	/**
	* Phase one: gate, validate, re-verify, then issue a one-time
	* confirmation bound to this exact target + mode + message.
	*
	* Pipeline (order per spec): allowWrite gate → message pre-validation
	* (≤16 KiB by bytes, non-empty, no NUL) → live target re-check →
	* injectable-agent whitelist → capacity check → token issuance.
	*/
	async prepare(req) {
		this.prune(this.now());
		const digest = digestMessage(req.message);
		if (!this.deps.allowWrite()) return this.rejectPrepare(req, digest, "inject_disabled");
		const invalid = validateMessage(req.message, digest.bytes);
		if (invalid !== null) return this.rejectPrepare(req, digest, "invalid_message", invalid);
		const target = {
			agent: req.target.agent,
			sessionId: req.target.sessionId
		};
		const status = await this.deps.verifyTarget(target);
		if (status === null) return this.rejectPrepare(req, digest, "target_not_found");
		if (status.status === "dead") return this.rejectPrepare(req, digest, "target_dead");
		if (this.executorFor(target.agent) === null) return this.rejectPrepare(req, digest, "unsupported_agent");
		const issuedAt = this.now();
		if (this.inFlightCount(issuedAt) >= 32) return this.rejectPrepare(req, digest, "too_many_pending");
		const requestId = this.randomId();
		const confirmToken = randomBytes(TOKEN_BYTES).toString("hex");
		const expiresAt = issuedAt + TOKEN_TTL_MS;
		this.pending.set(requestId, {
			token: confirmToken,
			target,
			mode: req.mode,
			messageSha256: digest.sha256,
			expiresAt,
			consumed: false
		});
		this.record({
			ts: issuedAt,
			phase: "prepare",
			requestId,
			target: { ...target },
			mode: req.mode,
			ok: true,
			messageBytes: digest.bytes,
			messageSha12: digest.sha12
		});
		return {
			ok: true,
			requestId,
			confirmToken,
			plan: {
				target: { ...target },
				mode: req.mode,
				targetStatus: { ...status },
				messagePreview: {
					bytes: digest.bytes,
					head: req.message.slice(0, HEAD_PREVIEW_CHARS)
				}
			},
			expiresAt
		};
	}
	/**
	* Phase two: validate the confirmation, dispatch to the path executor,
	* and cache the first result per requestId.
	*
	* Rejection order: cached replay (idempotency wins) → token missing →
	* token reused → token expired → token/message binding mismatch →
	* unsupported agent. Every attempt against a live token consumes it,
	* whatever happens afterwards.
	*/
	async execute(req) {
		const now = this.now();
		this.prune(now);
		const digest = digestMessage(req.message);
		const record = this.pending.get(req.requestId) ?? null;
		const cached = this.results.get(req.requestId);
		if (cached !== void 0) {
			if (record === null) return this.rejectExecute(req.requestId, null, digest, "token_missing");
			if (!tokenEquals(record.token, req.confirmToken) || record.messageSha256 !== digest.sha256) return this.rejectExecute(req.requestId, record, digest, "token_mismatch");
			const replay = {
				...cached.result,
				replayed: true
			};
			this.logExecuteResult(req.requestId, record, digest, replay);
			return replay;
		}
		if (!req.confirmToken) return this.rejectExecute(req.requestId, record, digest, "token_missing");
		if (record === null) return this.rejectExecute(req.requestId, null, digest, "token_missing");
		if (record.consumed) return this.rejectExecute(req.requestId, record, digest, "token_reused");
		if (record.expiresAt <= now) {
			this.pending.delete(req.requestId);
			return this.rejectExecute(req.requestId, record, digest, "token_expired");
		}
		record.consumed = true;
		if (!tokenEquals(record.token, req.confirmToken)) return this.rejectExecute(req.requestId, record, digest, "token_mismatch");
		if (record.messageSha256 !== digest.sha256) return this.rejectExecute(req.requestId, record, digest, "token_mismatch");
		const executor = this.executorFor(record.target.agent);
		if (executor === null) return this.rejectExecute(req.requestId, record, digest, "unsupported_agent");
		let result;
		try {
			result = await executor.execute({
				target: { ...record.target },
				mode: record.mode,
				message: req.message,
				requestId: req.requestId
			});
		} catch (err) {
			result = {
				outcome: "failed",
				errorCode: "executor_error",
				detail: err instanceof Error ? err.message : String(err)
			};
		}
		this.results.set(req.requestId, {
			result: { ...result },
			expiresAt: this.now() + RESULT_CACHE_TTL_MS
		});
		this.logExecuteResult(req.requestId, record, digest, result);
		return result;
	}
	/** Read-only audit view (newest first), for the M3 detail page. */
	getRecentLog(limit = DEFAULT_LOG_QUERY_LIMIT) {
		const bounded = Math.min(Math.max(Math.floor(limit), 0), this.logRing.length);
		return this.logRing.slice(this.logRing.length - bounded).reverse();
	}
	executorFor(agent) {
		if (agent === "dsh") return this.deps.executors.dsh;
		if (SEND_CLI_AGENTS.has(agent)) return this.deps.executors.sendCli;
		return null;
	}
	/** Issued-but-unconsumed-and-unexpired tokens count toward the cap. */
	inFlightCount(now) {
		let count = 0;
		for (const record of this.pending.values()) if (!record.consumed && record.expiresAt > now) count += 1;
		return count;
	}
	/**
	* Housekeeping. Pending records outlive their token TTL by the result
	* cache window so that late replays keep their binding check and late
	* reuse attempts still answer `token_reused` (not `token_missing`).
	*/
	prune(now) {
		for (const [id, record] of this.pending) if (record.expiresAt + 3e5 <= now) this.pending.delete(id);
		for (const [id, cached] of this.results) if (cached.expiresAt <= now) this.results.delete(id);
	}
	rejectPrepare(req, digest, errorCode, detail) {
		this.record({
			ts: this.now(),
			phase: "prepare",
			requestId: null,
			target: {
				agent: req.target.agent,
				sessionId: req.target.sessionId
			},
			mode: req.mode,
			ok: false,
			errorCode,
			messageBytes: digest.bytes,
			messageSha12: digest.sha12
		});
		return detail === void 0 ? {
			ok: false,
			errorCode
		} : {
			ok: false,
			errorCode,
			detail
		};
	}
	rejectExecute(requestId, record, digest, errorCode) {
		this.record({
			ts: this.now(),
			phase: "execute",
			requestId,
			target: record === null ? null : { ...record.target },
			mode: record === null ? null : record.mode,
			ok: false,
			outcome: "failed",
			errorCode,
			messageBytes: digest.bytes,
			messageSha12: digest.sha12
		});
		return {
			outcome: "failed",
			errorCode
		};
	}
	logExecuteResult(requestId, record, digest, result) {
		this.record({
			ts: this.now(),
			phase: "execute",
			requestId,
			target: record === null ? null : { ...record.target },
			mode: record === null ? null : record.mode,
			ok: result.outcome === "delivered",
			outcome: result.outcome,
			...result.errorCode !== void 0 ? { errorCode: result.errorCode } : {},
			...result.replayed !== void 0 ? { replayed: result.replayed } : {},
			messageBytes: digest.bytes,
			messageSha12: digest.sha12
		});
	}
	record(entry) {
		const frozen = Object.freeze({
			...entry,
			target: entry.target === null ? null : Object.freeze({ ...entry.target })
		});
		this.logRing.push(frozen);
		if (this.logRing.length > LOG_RING_LIMIT) this.logRing.splice(0, this.logRing.length - LOG_RING_LIMIT);
		this.deps.log(frozen);
	}
};
//#endregion
//#region src/guard.ts
const OK = { ok: true };
const forbid = (reason) => ({
	ok: false,
	status: 403,
	reason
});
/** Methods whose body is a state-changing payload (layer 4 media-type gate). */
const BODY_METHODS = /* @__PURE__ */ new Set([
	"POST",
	"PUT",
	"PATCH"
]);
/**
* True when `addr` (a `socket.remoteAddress` value) is a loopback address:
* IPv4 `127.0.0.0/8`, IPv6 `::1`, or the IPv4-mapped form `::ffff:127.x.y.z`
* that Node reports on dual-stack listeners. Anything unparsable is `false`
* (fail closed).
*/
function isLoopbackAddress(addr) {
	if (!addr) return false;
	let candidate = addr.trim().toLowerCase();
	if (candidate.startsWith("::ffff:")) candidate = candidate.slice(7);
	if (candidate === "::1") return true;
	return isLoopbackIpv4(candidate);
}
/** Strict dotted-quad check for `127.0.0.0/8`. */
function isLoopbackIpv4(candidate) {
	const parts = candidate.split(".");
	if (parts.length !== 4) return false;
	for (const part of parts) if (!/^\d{1,3}$/.test(part) || Number(part) > 255) return false;
	return parts[0] === "127";
}
/**
* Parse an authority string (`Host` header shape). Returns undefined for
* anything malformed: empty, bad brackets, non-numeric or out-of-range port,
* stray colons. Node keeps only the first `Host` header on duplicates, so a
* single string is the full input space here.
*/
function parseAuthority(raw) {
	if (typeof raw !== "string") return void 0;
	const value = raw.trim().toLowerCase();
	if (!value) return void 0;
	let host;
	let portPart;
	if (value.startsWith("[")) {
		const close = value.indexOf("]");
		if (close <= 1) return void 0;
		host = value.slice(0, close + 1);
		const rest = value.slice(close + 1);
		if (rest) {
			if (!rest.startsWith(":")) return void 0;
			portPart = rest.slice(1);
		}
	} else {
		const colon = value.indexOf(":");
		if (colon === -1) host = value;
		else {
			host = value.slice(0, colon);
			portPart = value.slice(colon + 1);
			if (portPart.includes(":")) return void 0;
		}
		if (!host || /[\s/@#?\\]/.test(host)) return void 0;
	}
	if (portPart !== void 0) {
		if (!/^\d{1,5}$/.test(portPart)) return void 0;
		const num = Number(portPart);
		if (num < 1 || num > 65535) return void 0;
	}
	return {
		host,
		port: portPart
	};
}
/** True when a parsed authority host names loopback. */
function authorityIsLoopback(host) {
	if (host === "localhost") return true;
	if (host.startsWith("[") && host.endsWith("]")) return isLoopbackAddress(host.slice(1, -1));
	return isLoopbackIpv4(host);
}
/**
* Same-origin check between an `Origin` header value and the request's
* `Host` authority. Scheme may be http or https; host must match exactly
* (WHATWG-normalized: lowercase, IPv6 canonical bracketed form) and the
* effective ports must agree. A `Host` without a port accepts either
* scheme-default origin port (80/443), covering default-port elision.
*/
function originMatchesAuthority(origin, authority) {
	let url;
	try {
		url = new URL(origin);
	} catch {
		return false;
	}
	if (url.protocol !== "http:" && url.protocol !== "https:") return false;
	if (url.hostname.toLowerCase() !== authority.host) return false;
	const originPort = url.port || (url.protocol === "https:" ? "443" : "80");
	if (authority.port !== void 0) return originPort === authority.port;
	return originPort === "80" || originPort === "443";
}
/** Reject when any (possibly `, `-joined multi-value) entry is `cross-site`. */
function declaresCrossSite(secFetchSite) {
	if (secFetchSite === void 0) return false;
	return (Array.isArray(secFetchSite) ? secFetchSite : [secFetchSite]).some((value) => value.split(",").some((entry) => entry.trim().toLowerCase() === "cross-site"));
}
/** Layers 1-3: remote loopback, Host authority, Origin/sec-fetch-site. */
function guardReachability(req) {
	if (!isLoopbackAddress(req.socket?.remoteAddress ?? void 0)) return forbid("remote_not_loopback");
	const hostHeader = req.headers.host;
	const authority = typeof hostHeader === "string" ? parseAuthority(hostHeader) : void 0;
	if (!authority || !authorityIsLoopback(authority.host)) return forbid("host_not_loopback");
	const origin = req.headers.origin;
	if (origin !== void 0) {
		if (Array.isArray(origin) || !originMatchesAuthority(origin, authority)) return forbid("origin_mismatch");
	}
	if (declaresCrossSite(req.headers["sec-fetch-site"])) return forbid("cross_site");
	return OK;
}
/**
* Full HTTP-route guard, layers 1-4 in order:
*
* 1. `socket.remoteAddress` must be loopback → else 403;
* 2. `Host` must be a loopback authority → else 403;
* 3. `Origin` (when present) must be same-origin with Host, and
*    `sec-fetch-site: cross-site` is explicitly refused → else 403;
* 4. POST/PUT/PATCH must carry `content-type: application/json` (charset
*    parameter allowed) → else 415.
*
* Layer 5 (the write-action gate) is {@link guardWriteAction}: routes call
* it only for state-changing actions, chaining this verdict through.
*
* @param req - the incoming request (or a structural mock in tests).
* @param _opts - reserved; layers 1-4 need no dynamic settings today.
*/
function guardRequest(req, _opts) {
	const reachability = guardReachability(req);
	if (!reachability.ok) return reachability;
	const method = (req.method ?? "").toUpperCase();
	if (BODY_METHODS.has(method)) {
		const contentType = req.headers["content-type"];
		if ((typeof contentType === "string" ? contentType.split(";", 1)[0]?.trim().toLowerCase() : void 0) !== "application/json") return {
			ok: false,
			status: 415,
			reason: "unsupported_media_type"
		};
	}
	return OK;
}
/**
* Layer 5 — write-action gate. Chains an earlier verdict (typically from
* {@link guardRequest}) and then requires `inject.enabled` to be on, read
* live via {@link GuardOptions.allowWriteActions}. The one-time confirmToken
* check is the M2 inject gateway's job, not this layer's.
*
* @param verdictCtx - verdict from the preceding layers; failures pass through.
* @param opts - dynamic settings source; gate is closed when it says so.
*/
function guardWriteAction(verdictCtx, opts) {
	if (!verdictCtx.ok) return verdictCtx;
	if (!opts.allowWriteActions()) return forbid("inject_disabled");
	return OK;
}
//#endregion
//#region src/routes.ts
/** Route namespace, per the `/plugins/<package>/` convention (design §4.f). */
const API_PREFIX = "/plugins/agent-sidecar/api";
const DEFAULT_MAX_SSE_CLIENTS = 8;
const DEFAULT_SSE_HEARTBEAT_MS = 15e3;
const DEFAULT_SSE_BUFFER_LIMIT = 256;
const HEARTBEAT_FRAME = ": hb\n\n";
/** Bound on the `POST action` JSON body (message cap is 16 KiB + envelope). */
const MAX_ACTION_BODY_BYTES = 64 * 1024;
/** `inject.prepare` rejection code → HTTP status (task spec mapping). */
const PREPARE_ERROR_STATUS = {
	inject_disabled: 403,
	invalid_message: 422,
	target_not_found: 404,
	target_dead: 409,
	too_many_pending: 429,
	unsupported_agent: 422
};
/**
* `inject.execute` failed-outcome code → HTTP status. Unlisted codes
* (executor-native vocab) and codeless failures fall back to 502.
*/
const EXECUTE_ERROR_STATUS = {
	token_missing: 401,
	token_expired: 401,
	token_reused: 409,
	token_mismatch: 409,
	unsupported_agent: 422,
	executor_error: 502
};
/**
* Analysis-engine error code → HTTP status (task spec mapping). `cancelled`
* stays 200: it is a terminal fact about the analysis session carried in
* the result outcome, not a transport failure. Unknown codes fall back to
* 502 like the execute map does.
*/
const ANALYSIS_ERROR_STATUS = {
	analysis_disabled: 403,
	too_many_active: 429,
	timeout: 504,
	create_failed: 502,
	cancelled: 200
};
const ANALYSIS_ACTION_TYPES = /* @__PURE__ */ new Set([
	"analysis.request",
	"analysis.followup",
	"analysis.cancel"
]);
function writeJson(res, status, body) {
	res.writeHead(status, {
		"content-type": "application/json; charset=utf-8",
		"cache-control": "no-store"
	});
	res.end(JSON.stringify(body));
}
function writeMethodNotAllowed(res, allow) {
	res.writeHead(405, {
		allow,
		"content-type": "application/json; charset=utf-8"
	});
	res.end(JSON.stringify({ reason: "method_not_allowed" }));
}
/**
* Path inside the namespace ('' for the bare prefix), or null when the
* request is outside {@link API_PREFIX} or the URL is unparsable. The
* carrier already parsed the same string to match the route, so the null
* arms only matter when `handle` is exercised directly.
*/
function subpathOf(rawUrl) {
	let pathname;
	try {
		pathname = new URL(rawUrl ?? "/", "http://dsh.internal").pathname;
	} catch {
		return null;
	}
	if (pathname === "/plugins/agent-sidecar/api") return "";
	if (pathname.startsWith(`/plugins/agent-sidecar/api/`)) return pathname.slice(27);
	return null;
}
/** Query string of the request (empty params when the URL is unparsable). */
function queryOf(rawUrl) {
	try {
		return new URL(rawUrl ?? "/", "http://dsh.internal").searchParams;
	} catch {
		return new URLSearchParams();
	}
}
/**
* Timeline pagination token: `<seq|'-'>~<epoch-ms>`. Deliberately not the
* raw JSON cursor object so the query-string round-trip stays trivial and
* the wire shape is decoupled from fusion's internal cursor type.
*/
function encodeCursor(cursor) {
	return `${cursor.seq === null ? "-" : cursor.seq}~${cursor.ts}`;
}
function decodeCursor(raw) {
	const sep = raw.indexOf("~");
	if (sep <= 0 || sep === raw.length - 1) return null;
	const seqPart = raw.slice(0, sep);
	const tsPart = raw.slice(sep + 1);
	const ts = Number(tsPart);
	if (!Number.isInteger(ts) || ts < 0) return null;
	if (seqPart === "-") return {
		seq: null,
		ts
	};
	const seq = Number(seqPart);
	if (!Number.isInteger(seq) || seq < 0) return null;
	return {
		seq,
		ts
	};
}
/** Bound on caller-supplied page sizes (timeline entries / search hits). */
const MAX_PAGE_LIMIT = 500;
/** Search result bound when the caller supplies no limit (fusion default). */
const DEFAULT_SEARCH_ROUTE_LIMIT = 50;
/** Match fusion's project correlation key: strip trailing slashes (keep `/`). */
function normalizeProjectKey(project) {
	if (project.length > 1 && project.endsWith("/")) {
		const stripped = project.replace(/\/+$/, "");
		return stripped === "" ? "/" : stripped;
	}
	return project;
}
/**
* Parse an optional positive-integer query param bounded by
* {@link MAX_PAGE_LIMIT}. `undefined` when absent, `null` when invalid.
*/
function parseLimit(params, name) {
	const raw = params.get(name);
	if (raw === null || raw === "") return void 0;
	const value = Number(raw);
	if (!Number.isInteger(value) || value < 1 || value > MAX_PAGE_LIMIT) return null;
	return value;
}
/** JSON wire shape of one timeline page (adds the encoded `nextCursor`). */
function timelineBody(page) {
	return {
		sessionId: page.sessionId,
		entries: page.entries,
		cursor: page.cursor,
		nextCursor: page.cursor === null ? null : encodeCursor(page.cursor),
		sources: page.sources
	};
}
/** `event: <name>` + single-line JSON data (JSON.stringify never emits raw newlines). */
function sseFrame(event, data) {
	return `event: ${event}\ndata: ${data}\n\n`;
}
/**
* Read the request body up to {@link MAX_ACTION_BODY_BYTES}. On overflow the
* promise settles immediately ('too_large') while the rest of the stream
* keeps draining, so the keep-alive connection is left in a clean state.
*/
function readActionBody(req) {
	return new Promise((resolve) => {
		const chunks = [];
		let size = 0;
		let settled = false;
		const settle = (result) => {
			if (settled) return;
			settled = true;
			resolve(result);
		};
		req.on("data", (chunk) => {
			if (settled) return;
			const buf = typeof chunk === "string" ? Buffer.from(chunk, "utf8") : chunk;
			size += buf.length;
			if (size > 65536) {
				settle({ kind: "too_large" });
				return;
			}
			chunks.push(buf);
		});
		req.on("end", () => settle({
			kind: "ok",
			text: Buffer.concat(chunks).toString("utf8")
		}));
		req.on("error", () => settle({ kind: "error" }));
	});
}
/**
* Build the M1 route surface. All state lives in the returned closure;
* multiple instances never share anything.
*/
function createRoutes(deps, opts = {}) {
	const maxSseClients = opts.maxSseClients ?? DEFAULT_MAX_SSE_CLIENTS;
	const sseHeartbeatMs = opts.sseHeartbeatMs ?? DEFAULT_SSE_HEARTBEAT_MS;
	const sseBufferLimit = opts.sseBufferLimit ?? DEFAULT_SSE_BUFFER_LIMIT;
	const clients = /* @__PURE__ */ new Set();
	let disposed = false;
	const buildSnapshot = () => ({
		daemon: {
			state: deps.supervisor.state,
			lastPing: deps.supervisor.lastPing
		},
		board: deps.store.getBoardState(),
		capabilities: { inject: deps.guardOptions.allowWriteActions() }
	});
	const cleanupClient = (client) => {
		if (client.closed) return;
		client.closed = true;
		if (client.heartbeat !== null) clearInterval(client.heartbeat);
		client.heartbeat = null;
		client.pending.length = 0;
		clients.delete(client);
		deps.log("info", "sse client disconnected", { clients: clients.size });
	};
	const dropClient = (client, reason) => {
		deps.log("warn", "sse client dropped", {
			reason,
			pending: client.pending.length,
			limit: sseBufferLimit
		});
		cleanupClient(client);
		client.res.destroy();
	};
	const push = (client, frame) => {
		if (client.closed) return;
		if (client.blocked) {
			client.pending.push(frame);
			if (client.pending.length > sseBufferLimit) dropClient(client, "buffer_overflow");
			return;
		}
		if (!client.res.write(frame)) client.blocked = true;
	};
	const flush = (client) => {
		if (client.closed) return;
		client.blocked = false;
		while (!client.blocked) {
			const frame = client.pending.shift();
			if (frame === void 0) return;
			if (!client.res.write(frame)) client.blocked = true;
		}
	};
	const acceptStream = (res) => {
		if (clients.size >= maxSseClients) {
			deps.log("warn", "sse connection rejected: client limit reached", { max: maxSseClients });
			writeJson(res, 503, { reason: "too_many_stream_clients" });
			return;
		}
		res.writeHead(200, {
			"content-type": "text/event-stream",
			"cache-control": "no-cache",
			connection: "keep-alive"
		});
		const client = {
			res,
			pending: [],
			blocked: false,
			closed: false,
			heartbeat: null
		};
		clients.add(client);
		res.on("close", () => cleanupClient(client));
		res.on("drain", () => flush(client));
		client.heartbeat = setInterval(() => push(client, HEARTBEAT_FRAME), sseHeartbeatMs);
		deps.log("info", "sse client connected", { clients: clients.size });
		push(client, sseFrame("state", JSON.stringify(buildSnapshot())));
	};
	/** One change → one full snapshot frame to every client (M1 granularity). */
	const onMutation = () => {
		if (disposed || clients.size === 0) return;
		const frame = sseFrame("state", JSON.stringify(buildSnapshot()));
		for (const client of [...clients]) push(client, frame);
	};
	const unsubscribes = [deps.store.onChange(onMutation), deps.supervisor.onStateChange(onMutation)];
	/** Decoded session id, or null (already answered 404) on a bad escape. */
	const decodeId = (res, rawId) => {
		try {
			const id = decodeURIComponent(rawId);
			if (id !== "") return id;
		} catch {}
		writeJson(res, 404, { reason: "session_not_found" });
		return null;
	};
	const handleSession = async (res, rawId) => {
		const id = decodeId(res, rawId);
		if (id === null) return;
		const view = deps.store.getBoardState().sessions.find((s) => s.session_id === id);
		const fusion = deps.fusion;
		if (fusion === void 0) {
			if (view === void 0) {
				writeJson(res, 404, { reason: "session_not_found" });
				return;
			}
			writeJson(res, 200, {
				session: view,
				timeline: null,
				timelineNote: "timeline_not_available_until_m3"
			});
			return;
		}
		const unified = fusion.getUnifiedSessions().find((s) => s.sessionId === id) ?? null;
		if (view === void 0 && unified === null) {
			writeJson(res, 404, { reason: "session_not_found" });
			return;
		}
		const page = await fusion.getSessionTimeline(id);
		writeJson(res, 200, {
			session: view ?? null,
			unified,
			timeline: timelineBody(page)
		});
	};
	const handleTimeline = async (res, rawId, params) => {
		const fusion = deps.fusion;
		if (fusion === void 0) {
			writeJson(res, 501, { reason: "fusion_not_wired" });
			return;
		}
		const id = decodeId(res, rawId);
		if (id === null) return;
		const rawCursor = params.get("cursor");
		let before = null;
		if (rawCursor !== null && rawCursor !== "") {
			before = decodeCursor(rawCursor);
			if (before === null) {
				writeJson(res, 400, { reason: "invalid_cursor" });
				return;
			}
		}
		const limit = parseLimit(params, "limit");
		if (limit === null) {
			writeJson(res, 400, { reason: "invalid_limit" });
			return;
		}
		const page = await fusion.getSessionTimeline(id, {
			before,
			limit
		});
		if (!(page.sources.dshLive || page.sources.dshCold || page.sources.sidecarReplay || page.sources.sidecarBuffer) && page.entries.length === 0) {
			if (!(deps.store.getBoardState().sessions.some((s) => s.session_id === id) || fusion.getUnifiedSessions().some((s) => s.sessionId === id))) {
				writeJson(res, 404, { reason: "session_not_found" });
				return;
			}
		}
		writeJson(res, 200, timelineBody(page));
	};
	const handleLineage = async (res, rawId) => {
		const fusion = deps.fusion;
		if (fusion === void 0) {
			writeJson(res, 501, { reason: "fusion_not_wired" });
			return;
		}
		const id = decodeId(res, rawId);
		if (id === null) return;
		writeJson(res, 200, await fusion.getLineage(id));
	};
	const handleSearch = async (res, params) => {
		const fusion = deps.fusion;
		if (fusion === void 0) {
			writeJson(res, 501, { reason: "fusion_not_wired" });
			return;
		}
		const query = (params.get("q") ?? "").trim();
		const project = (params.get("project") ?? "").trim();
		if (query === "" && project === "") {
			writeJson(res, 400, {
				reason: "invalid_request",
				detail: "search needs q= (text query) and/or project= (project filter)"
			});
			return;
		}
		const limit = parseLimit(params, "limit");
		if (limit === null) {
			writeJson(res, 400, { reason: "invalid_limit" });
			return;
		}
		let mode;
		let items;
		if (query !== "") {
			const result = await fusion.searchSessions(query, limit === void 0 ? {} : { limit });
			mode = result.mode;
			items = result.items;
		} else {
			mode = "filter-only";
			items = fusion.getUnifiedSessions().map((session) => ({
				session,
				matchedBy: "project",
				snippet: null
			}));
		}
		if (project !== "") {
			const wanted = normalizeProjectKey(project);
			items = items.filter((item) => normalizeProjectKey(item.session.project) === wanted);
		}
		items = items.slice(0, limit ?? DEFAULT_SEARCH_ROUTE_LIMIT);
		writeJson(res, 200, {
			mode,
			query,
			project: project === "" ? null : project,
			items
		});
	};
	const handleProjects = (res) => {
		const fusion = deps.fusion;
		if (fusion === void 0) {
			writeJson(res, 501, { reason: "fusion_not_wired" });
			return;
		}
		writeJson(res, 200, { groups: fusion.getProjectGroups() });
	};
	/**
	* Route-log discipline (S8): only the action type, status and vocabulary
	* codes — never the message body, preview, or gateway detail text.
	*/
	const logAction = (type, status, meta = {}) => {
		deps.log("info", "action handled", {
			type,
			status,
			...meta
		});
	};
	const handlePrepare = async (gateway, envelope, res) => {
		const rawTarget = envelope.target;
		const targetObj = typeof rawTarget === "object" && rawTarget !== null ? rawTarget : void 0;
		const agent = targetObj?.agent;
		const sessionId = targetObj?.sessionId;
		const mode = envelope.mode;
		const message = envelope.message;
		if (typeof agent !== "string" || typeof sessionId !== "string" || mode !== "queue" && mode !== "steer" || typeof message !== "string") {
			logAction("inject.prepare", 400, { reason: "invalid_request" });
			writeJson(res, 400, {
				reason: "invalid_request",
				detail: "inject.prepare needs target{agent,sessionId}, mode queue|steer, and a string message"
			});
			return;
		}
		const result = await gateway.prepare({
			target: {
				agent,
				sessionId
			},
			mode,
			message
		});
		if (result.ok) {
			logAction("inject.prepare", 200, { requestId: result.requestId });
			writeJson(res, 200, {
				requestId: result.requestId,
				confirmToken: result.confirmToken,
				plan: result.plan,
				expiresAt: result.expiresAt
			});
			return;
		}
		const status = PREPARE_ERROR_STATUS[result.errorCode] ?? 400;
		logAction("inject.prepare", status, { errorCode: result.errorCode });
		writeJson(res, status, {
			reason: result.errorCode,
			...result.detail !== void 0 ? { detail: result.detail } : {}
		});
	};
	const handleExecute = async (gateway, envelope, res) => {
		const { requestId, confirmToken, message } = envelope;
		if (typeof requestId !== "string" || typeof confirmToken !== "string" || typeof message !== "string") {
			logAction("inject.execute", 400, { reason: "invalid_request" });
			writeJson(res, 400, {
				reason: "invalid_request",
				detail: "inject.execute needs string requestId, confirmToken and message"
			});
			return;
		}
		const result = await gateway.execute({
			requestId,
			confirmToken,
			message
		});
		const status = result.outcome === "failed" ? EXECUTE_ERROR_STATUS[result.errorCode ?? ""] ?? 502 : 200;
		logAction("inject.execute", status, {
			outcome: result.outcome,
			...result.errorCode !== void 0 ? { errorCode: result.errorCode } : {},
			...result.replayed !== void 0 ? { replayed: result.replayed } : {}
		});
		writeJson(res, status, result);
	};
	/**
	* Answer one engine result: status per {@link ANALYSIS_ERROR_STATUS},
	* body is the result verbatim (it already carries outcome /
	* analysisSessionId / summary / truncated / disclaimer). The log line
	* keeps only outcome/codes/ids — never summaries or questions (S8).
	*/
	const respondAnalysisResult = (type, res, result) => {
		const status = result.errorCode !== void 0 ? ANALYSIS_ERROR_STATUS[result.errorCode] ?? 502 : 200;
		logAction(type, status, {
			outcome: result.outcome,
			...result.errorCode !== void 0 ? { errorCode: result.errorCode } : {},
			...result.analysisSessionId !== void 0 ? { analysisSessionId: result.analysisSessionId } : {},
			...result.truncated ? { truncated: true } : {}
		});
		writeJson(res, status, result);
	};
	const rejectInvalidAnalysis = (type, res, detail) => {
		logAction(type, 400, { reason: "invalid_request" });
		writeJson(res, 400, {
			reason: "invalid_request",
			detail
		});
	};
	const handleAnalysisRequest = async (analysis, envelope, res) => {
		const { targetKind, targetId, question } = envelope;
		if (targetKind !== "session" && targetKind !== "project" && targetKind !== "cross-agent" || targetId !== void 0 && typeof targetId !== "string" || question !== void 0 && typeof question !== "string") {
			rejectInvalidAnalysis("analysis.request", res, "analysis.request needs targetKind session|project|cross-agent, optional string targetId and question");
			return;
		}
		if ((targetKind === "session" || targetKind === "project") && (targetId === void 0 || targetId === "")) {
			rejectInvalidAnalysis("analysis.request", res, `analysis.request with targetKind ${targetKind} needs a non-empty targetId`);
			return;
		}
		const input = await analysis.buildInput({
			targetKind,
			...targetId !== void 0 ? { targetId } : {},
			...question !== void 0 ? { question } : {}
		});
		if (input === null) {
			logAction("analysis.request", 404, {
				reason: "target_not_found",
				targetKind
			});
			writeJson(res, 404, { reason: "target_not_found" });
			return;
		}
		respondAnalysisResult("analysis.request", res, await analysis.engine.request(input));
	};
	const handleAnalysisFollowup = async (analysis, envelope, res) => {
		const { analysisSessionId, question } = envelope;
		if (typeof analysisSessionId !== "string" || analysisSessionId === "" || typeof question !== "string" || question === "") {
			rejectInvalidAnalysis("analysis.followup", res, "analysis.followup needs non-empty string analysisSessionId and question");
			return;
		}
		respondAnalysisResult("analysis.followup", res, await analysis.engine.followup(analysisSessionId, question));
	};
	const handleAnalysisCancel = async (analysis, envelope, res) => {
		const { analysisSessionId } = envelope;
		if (typeof analysisSessionId !== "string" || analysisSessionId === "") {
			rejectInvalidAnalysis("analysis.cancel", res, "analysis.cancel needs a non-empty string analysisSessionId");
			return;
		}
		await analysis.engine.cancel(analysisSessionId);
		logAction("analysis.cancel", 200, { analysisSessionId });
		writeJson(res, 200, {
			ok: true,
			analysisSessionId
		});
	};
	/** M2 dispatcher over the action envelope (gateway present, guard 1-4 passed). */
	const handleAction = async (gateway, verdict, req, res) => {
		const body = await readActionBody(req);
		if (body.kind === "too_large") {
			deps.log("warn", "action rejected", {
				reason: "body_too_large",
				limit: MAX_ACTION_BODY_BYTES
			});
			writeJson(res, 400, { reason: "body_too_large" });
			return;
		}
		if (body.kind === "error") {
			deps.log("warn", "action rejected", { reason: "body_read_error" });
			writeJson(res, 400, { reason: "body_read_error" });
			return;
		}
		let parsed;
		try {
			parsed = JSON.parse(body.text);
		} catch {
			deps.log("warn", "action rejected", { reason: "invalid_json" });
			writeJson(res, 400, { reason: "invalid_json" });
			return;
		}
		const envelope = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed : null;
		if (envelope !== null) {
			const type = typeof envelope.type === "string" ? envelope.type : null;
			if (type === "daemon.retry") {
				deps.supervisor.retry();
				const state = deps.supervisor.state;
				logAction("daemon.retry", 200, { state });
				writeJson(res, 200, { state });
				return;
			}
			if (type === "inject.prepare" || type === "inject.execute") {
				const writeVerdict = guardWriteAction(verdict, deps.guardOptions);
				if (!writeVerdict.ok) {
					logAction(type, writeVerdict.status, { reason: writeVerdict.reason });
					writeJson(res, writeVerdict.status, { reason: writeVerdict.reason });
					return;
				}
				if (type === "inject.prepare") await handlePrepare(gateway, envelope, res);
				else await handleExecute(gateway, envelope, res);
				return;
			}
			if (type !== null && ANALYSIS_ACTION_TYPES.has(type)) {
				if (type !== "analysis.cancel" && (deps.analysisEnabled === void 0 || !deps.analysisEnabled())) {
					logAction(type, 403, { reason: "analysis_disabled" });
					writeJson(res, 403, { reason: "analysis_disabled" });
					return;
				}
				const analysis = deps.analysis;
				if (analysis === void 0 || !analysis.available()) {
					logAction(type, 501, { reason: "analysis_unavailable" });
					writeJson(res, 501, { reason: "analysis_unavailable" });
					return;
				}
				if (type === "analysis.request" && analysis.modelConfigured !== void 0 && !analysis.modelConfigured()) {
					logAction(type, 403, { reason: "analysis_model_unconfigured" });
					writeJson(res, 403, { reason: "analysis_model_unconfigured" });
					return;
				}
				if (type === "analysis.request") await handleAnalysisRequest(analysis, envelope, res);
				else if (type === "analysis.followup") await handleAnalysisFollowup(analysis, envelope, res);
				else await handleAnalysisCancel(analysis, envelope, res);
				return;
			}
		}
		deps.log("warn", "action rejected", { reason: "unknown_action" });
		writeJson(res, 400, { reason: "unknown_action" });
	};
	const handle = async (req, res) => {
		if (disposed) {
			writeJson(res, 503, { reason: "shutting_down" });
			return;
		}
		const verdict = guardRequest(req, deps.guardOptions);
		const method = (req.method ?? "").toUpperCase();
		const subpath = subpathOf(req.url);
		if (!(verdict.ok && subpath === "action" && method === "POST" && deps.injectGateway !== void 0) && (method === "POST" || method === "PUT" || method === "PATCH")) req.resume();
		if (!verdict.ok) {
			writeJson(res, verdict.status, { reason: verdict.reason });
			return;
		}
		if (subpath === null || subpath === "") {
			writeJson(res, 404, { reason: "not_found" });
			return;
		}
		if (subpath === "state") {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			writeJson(res, 200, buildSnapshot());
			return;
		}
		if (subpath === "stream") {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			acceptStream(res);
			return;
		}
		if (subpath === "action") {
			if (method !== "POST") return writeMethodNotAllowed(res, "POST");
			const gateway = deps.injectGateway;
			if (gateway === void 0) {
				const writeVerdict = guardWriteAction(verdict, deps.guardOptions);
				if (!writeVerdict.ok) {
					writeJson(res, writeVerdict.status, { reason: writeVerdict.reason });
					return;
				}
				writeJson(res, 501, { reason: "not_implemented_until_m2" });
				return;
			}
			await handleAction(gateway, verdict, req, res);
			return;
		}
		if (subpath === "projects") {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			handleProjects(res);
			return;
		}
		if (subpath === "search") {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			await handleSearch(res, queryOf(req.url));
			return;
		}
		if (subpath.startsWith("lineage/")) {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			await handleLineage(res, subpath.slice(8));
			return;
		}
		if (subpath.startsWith("session/")) {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			const rest = subpath.slice(8);
			if (rest.endsWith("/timeline")) {
				await handleTimeline(res, rest.slice(0, -9), queryOf(req.url));
				return;
			}
			await handleSession(res, rest);
			return;
		}
		writeJson(res, 404, { reason: "not_found" });
	};
	const dispose = () => {
		if (disposed) return;
		disposed = true;
		for (const unsubscribe of unsubscribes) unsubscribe();
		for (const client of [...clients]) {
			cleanupClient(client);
			client.res.end();
		}
		deps.log("info", "routes disposed");
	};
	return {
		handle,
		dispose
	};
}
//#endregion
//#region src/send-cli.ts
const DEFAULT_SEND_CLI_COMMAND = Object.freeze(["agent-sidecar"]);
/** Detail cap for collected stderr (2 KiB). */
const STDERR_DETAIL_BYTES = 2 * 1024;
/** send --json responses are ≤4 MiB; anything past this is garbage. */
const MAX_STDOUT_BYTES = 8 * 1024 * 1024;
/** sidecar/inject.py MAX_SEND_TIMEOUT_SECONDS. */
const MAX_CLI_TIMEOUT_SECONDS = 900;
/** Byte-bounded chunk accumulator; excess input is dropped, not buffered. */
var BoundedCollector = class {
	limit;
	chunks = [];
	size = 0;
	constructor(limit) {
		this.limit = limit;
	}
	append(chunk) {
		if (this.size >= this.limit) return;
		const buf = typeof chunk === "string" ? Buffer.from(chunk, "utf8") : Buffer.from(chunk);
		const room = this.limit - this.size;
		const kept = buf.byteLength > room ? buf.subarray(0, room) : buf;
		this.chunks.push(Buffer.from(kept));
		this.size += kept.byteLength;
	}
	get bytes() {
		return this.size;
	}
	text() {
		return Buffer.concat(this.chunks).toString("utf8");
	}
};
/**
* Parse stdout as one `send --json` receipt. Anything that is not a JSON
* object carrying a valid `delivery` field yields null (exit-code fallback).
*/
function parseReceipt(stdoutText) {
	const trimmed = stdoutText.trim();
	if (!trimmed) return null;
	let value;
	try {
		value = JSON.parse(trimmed);
	} catch {
		return null;
	}
	if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
	const record = value;
	const delivery = record["delivery"];
	if (delivery !== "delivered" && delivery !== "unknown") return null;
	const rawErrorCode = record["error_code"];
	const errorCode = typeof rawErrorCode === "string" && rawErrorCode !== "" ? rawErrorCode : void 0;
	return {
		delivery,
		...errorCode !== void 0 ? { errorCode } : {},
		replayed: record["replayed"] === true
	};
}
function describeError$1(error) {
	return error instanceof Error ? error.message : String(error);
}
function createSendCliExecutor(deps) {
	const command = deps.opts?.command ?? DEFAULT_SEND_CLI_COMMAND;
	const timeoutMs = deps.opts?.timeoutMs ?? 3e4;
	const bufferMs = deps.opts?.hardTimeoutBufferMs ?? 5e3;
	const log = deps.log ?? (() => {});
	const cliTimeoutSecs = Math.min(MAX_CLI_TIMEOUT_SECONDS, Math.max(1, Math.floor(timeoutMs / 1e3)));
	const hardTimeoutMs = timeoutMs + bufferMs;
	return {
		kind: "send-cli",
		async execute(req) {
			const argv = [
				...command,
				"send",
				req.target.sessionId,
				"--message-stdin",
				"--allow-write",
				"--json",
				"--request-id",
				req.requestId,
				"--timeout",
				String(cliTimeoutSecs)
			];
			log("debug", "spawning sidecar send CLI", {
				requestId: req.requestId,
				agent: req.target.agent,
				sessionId: req.target.sessionId,
				mode: req.mode,
				timeoutSecs: cliTimeoutSecs
			});
			let proc;
			try {
				proc = deps.spawn(argv);
			} catch (error) {
				log("warn", "send CLI spawn failed", {
					requestId: req.requestId,
					error: describeError$1(error)
				});
				return {
					outcome: "failed",
					errorCode: "cli_not_found",
					detail: describeError$1(error)
				};
			}
			const stdout = new BoundedCollector(MAX_STDOUT_BYTES);
			const stderr = new BoundedCollector(STDERR_DETAIL_BYTES);
			proc.onStdout((chunk) => stdout.append(chunk));
			proc.onStderr((chunk) => stderr.append(chunk));
			try {
				proc.stdin.write(Buffer.from(req.message, "utf8"));
				proc.stdin.end();
			} catch {}
			let timer;
			const settled = await Promise.race([proc.exited.then((code) => ({
				kind: "exit",
				code
			}), (error) => ({
				kind: "spawn-error",
				error
			})), new Promise((resolve) => {
				timer = setTimeout(() => resolve({ kind: "timeout" }), hardTimeoutMs);
			})]);
			if (timer !== void 0) clearTimeout(timer);
			const stderrText = stderr.text();
			if (settled.kind === "timeout") {
				try {
					proc.kill();
				} catch {}
				log("warn", "send CLI hard timeout; process killed", {
					requestId: req.requestId,
					hardTimeoutMs
				});
				return {
					outcome: "unknown",
					errorCode: "timeout",
					detail: stderrText ? `no exit within ${hardTimeoutMs}ms; killed; stderr: ${stderrText}` : `no exit within ${hardTimeoutMs}ms; killed`
				};
			}
			if (settled.kind === "spawn-error") {
				log("warn", "send CLI could not be started", {
					requestId: req.requestId,
					error: describeError$1(settled.error)
				});
				return {
					outcome: "failed",
					errorCode: "cli_not_found",
					detail: describeError$1(settled.error)
				};
			}
			const receipt = parseReceipt(stdout.text());
			log("info", "send CLI exited", {
				requestId: req.requestId,
				agent: req.target.agent,
				sessionId: req.target.sessionId,
				exitCode: settled.code,
				parsedReceipt: receipt !== null,
				...receipt !== null ? {
					delivery: receipt.delivery,
					...receipt.errorCode !== void 0 ? { errorCode: receipt.errorCode } : {},
					replayed: receipt.replayed
				} : {},
				stdoutBytes: stdout.bytes,
				stderrBytes: stderr.bytes
			});
			if (receipt !== null) return {
				outcome: receipt.delivery === "delivered" ? "delivered" : "unknown",
				...receipt.errorCode !== void 0 ? { errorCode: receipt.errorCode } : {},
				...receipt.replayed ? { replayed: true } : {},
				...stderrText ? { detail: stderrText } : {}
			};
			const code = settled.code;
			if (code === 0) return {
				outcome: "delivered",
				detail: stderrText ? `parse_warning: exit 0 but stdout was not a send --json receipt; stderr: ${stderrText}` : "parse_warning: exit 0 but stdout was not a send --json receipt"
			};
			const base = { outcome: "failed" };
			if (code === 2) base.errorCode = "usage_error";
			else if (code === 130) base.errorCode = "interrupted";
			else if (code !== 1) base.errorCode = `exit_${code ?? "signal"}`;
			if (stderrText) base.detail = stderrText;
			return base;
		}
	};
}
//#endregion
//#region src/session-store.ts
/** Bound for the last-event text summary kept per session. */
const EVENT_TEXT_LIMIT = 160;
function sessionKey(agent, sessionId) {
	return `${agent}\u0000${sessionId}`;
}
function truncate(text, limit) {
	return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}
/**
* Extract a usable seq cursor. Mirrors `_sequence` in
* `sidecar/adapters/dsh.py` (integer only); JSON cannot distinguish
* `3.0` from `3` on the JS side, so `Number.isInteger` is the closest
* faithful check.
*/
function extractSeq(extra) {
	const value = extra["seq"];
	return typeof value === "number" && Number.isInteger(value) ? value : null;
}
/** In-memory session cache reconciled by snapshots, hinted by events. */
var SessionStore = class {
	rows = /* @__PURE__ */ new Map();
	eventState = /* @__PURE__ */ new Map();
	streamHealth = "unknown";
	lastReconcileAt = null;
	listeners = /* @__PURE__ */ new Set();
	/** Replace the full session set with an authoritative snapshot. */
	applySnapshot(rows) {
		const next = /* @__PURE__ */ new Map();
		for (const row of rows) {
			if (typeof row.session_id !== "string" || row.session_id === "") continue;
			next.set(sessionKey(row.agent, row.session_id), row);
		}
		this.rows = next;
		const staleKeys = [];
		for (const [key, state] of this.eventState) if (next.has(key)) state.gap = false;
		else staleKeys.push(key);
		for (const key of staleKeys) this.eventState.delete(key);
		this.lastReconcileAt = Date.now();
		this.notify();
	}
	/** Fold one stream event in as a hint (summary + seq continuity only). */
	applyEvent(ev) {
		const key = sessionKey(ev.agent, ev.session_id);
		let state = this.eventState.get(key);
		if (state === void 0) {
			state = {
				lastEvent: null,
				lastSeq: null,
				gap: false
			};
			this.eventState.set(key, state);
		}
		state.lastEvent = {
			ts: ev.ts,
			kind: ev.kind,
			text: truncate(ev.text, EVENT_TEXT_LIMIT)
		};
		const seq = extractSeq(ev.extra);
		if (seq !== null) {
			if (state.lastSeq !== null && seq > state.lastSeq + 1) state.gap = true;
			state.lastSeq = seq;
		}
		this.notify();
	}
	/** Stream health is owned by the Reconciler; the store just exposes it. */
	setStreamHealth(health) {
		if (this.streamHealth === health) return;
		this.streamHealth = health;
		this.notify();
	}
	/** True when any snapshot session is currently `working` (active cadence). */
	hasWorkingSessions() {
		for (const row of this.rows.values()) if (row.status === "working") return true;
		return false;
	}
	getBoardState() {
		const sessions = [];
		for (const [key, row] of this.rows) {
			const state = this.eventState.get(key);
			sessions.push({
				agent: row.agent,
				session_id: row.session_id,
				status: row.status,
				title: row.title,
				project: row.project,
				updated_at: row.updated_at,
				last_event: state?.lastEvent ?? null,
				gap: state?.gap ?? false
			});
		}
		sessions.sort((a, b) => b.updated_at - a.updated_at || a.session_id.localeCompare(b.session_id));
		return {
			sessions,
			streamHealth: this.streamHealth,
			lastReconcileAt: this.lastReconcileAt
		};
	}
	/** Subscribe to store mutations; returns the unsubscribe function. */
	onChange(cb) {
		this.listeners.add(cb);
		return () => {
			this.listeners.delete(cb);
		};
	}
	notify() {
		for (const listener of this.listeners) listener();
	}
};
//#endregion
//#region src/supervisor.ts
const defaultSetTimeout = (fn, ms) => globalThis.setTimeout(fn, ms);
const defaultClearTimeout = (handle) => {
	globalThis.clearTimeout(handle);
};
const describeError = (error) => error instanceof Error ? error.message : String(error);
var DaemonSupervisor = class {
	deps;
	opts;
	_state = "probe";
	_lastPing = null;
	listeners = /* @__PURE__ */ new Set();
	timers = /* @__PURE__ */ new Set();
	/** Only ever non-null for a process this supervisor spawned itself. */
	proc = null;
	/**
	* Invalidation token for async continuations (ping/detect results, process
	* exit watchers): each macro transition bumps it, so continuations started
	* under an older epoch abandon instead of acting on a stale world.
	*/
	epoch = 0;
	started = false;
	stopped = false;
	/** Consecutive hosting failures (readiness timeout or early exit). */
	hostFailures = 0;
	/** Consecutive ADOPTED re-ping misses. */
	pingFailures = 0;
	constructor(deps, options) {
		this.deps = deps;
		this.opts = {
			policy: options.policy,
			backoffLimit: options.backoffLimit ?? 5,
			backoffBaseMs: options.backoffBaseMs ?? 1e3,
			backoffCapMs: options.backoffCapMs ?? 3e4,
			probeIntervalMs: options.probeIntervalMs ?? 5e3,
			adoptedRepingMs: options.adoptedRepingMs ?? 5e3,
			adoptedFailureLimit: options.adoptedFailureLimit ?? 3,
			hostReadyTimeoutMs: options.hostReadyTimeoutMs ?? 5e3,
			hostReadyPingIntervalMs: options.hostReadyPingIntervalMs ?? 500
		};
	}
	get state() {
		return this._state;
	}
	/** Last successful ping payload; null until the daemon answered once. */
	get lastPing() {
		return this._lastPing;
	}
	/** Subscribe to state transitions; returns an unsubscribe function. */
	onStateChange(listener) {
		this.listeners.add(listener);
		return () => {
			this.listeners.delete(listener);
		};
	}
	start() {
		if (this.started || this.stopped) return;
		this.started = true;
		if (this.opts.policy === "off") {
			this.deps.log("info", "daemon management policy is off; supervisor standing down");
			this.setState("defer");
			return;
		}
		this.runDetermination("probe");
	}
	/**
	* Tear everything down for the ctx.effect disposer: probe/backoff timers
	* first, then terminate a self-spawned daemon. Adopted or launchd-managed
	* daemons are never touched. Idempotent.
	*/
	async stop() {
		if (this.stopped) return;
		this.stopped = true;
		this.epoch += 1;
		this.clearAllTimers();
		this.listeners.clear();
		const proc = this.proc;
		this.proc = null;
		if (proc) {
			this.deps.log("info", "stopping supervisor: terminating self-hosted daemon");
			try {
				await proc.terminate();
			} catch (error) {
				this.deps.log("warn", "terminate during stop failed", { error: describeError(error) });
			}
		}
	}
	/** From FAILED only: reset the failure budget and re-run the determination. */
	retry() {
		if (this.stopped) return;
		if (this._state !== "failed") {
			this.deps.log("debug", "retry ignored outside FAILED state", { state: this._state });
			return;
		}
		this.hostFailures = 0;
		this.pingFailures = 0;
		this.deps.log("info", "retry requested: failure budget reset, re-probing");
		this.runDetermination("probe");
	}
	/** Shared PROBE/REPROBE determination: ping → adopt, else LaunchAgent → defer, else host. */
	async runDetermination(entry) {
		const ep = ++this.epoch;
		this.clearAllTimers();
		this.setState(entry);
		const info = await this.safePing();
		if (this.invalidated(ep)) return;
		if (info) {
			this.enterAdopted(info);
			return;
		}
		let managed = false;
		try {
			managed = await this.deps.detectLaunchAgent();
		} catch (error) {
			this.deps.log("warn", "LaunchAgent detection failed; assuming absent", { error: describeError(error) });
		}
		if (this.invalidated(ep)) return;
		if (managed) {
			this.enterDefer("LaunchAgent installed; launchd owns daemon liveness");
			return;
		}
		if (this.opts.policy === "adopt-only") {
			this.enterDefer("policy adopt-only forbids spawning");
			return;
		}
		this.enterHosting();
	}
	enterAdopted(info) {
		this.clearAllTimers();
		this._lastPing = info;
		this.pingFailures = 0;
		this.hostFailures = 0;
		this.deps.log("info", "adopted existing daemon", {
			pid: info.pid,
			version: info.version
		});
		this.setState("adopted");
		this.scheduleAdoptedPing();
	}
	scheduleAdoptedPing() {
		this.schedule(this.opts.adoptedRepingMs, () => {
			this.adoptedPing();
		});
	}
	async adoptedPing() {
		const ep = this.epoch;
		const info = await this.safePing();
		if (this.invalidated(ep) || this._state !== "adopted") return;
		if (info) {
			this._lastPing = info;
			this.pingFailures = 0;
			this.scheduleAdoptedPing();
			return;
		}
		this.pingFailures += 1;
		this.deps.log("warn", "adopted daemon missed ping", {
			misses: this.pingFailures,
			limit: this.opts.adoptedFailureLimit
		});
		if (this.pingFailures >= this.opts.adoptedFailureLimit) {
			this.pingFailures = 0;
			this.runDetermination("reprobe");
			return;
		}
		this.scheduleAdoptedPing();
	}
	/** External management (launchd, or adopt-only policy): re-probe periodically, never spawn. */
	enterDefer(reason) {
		this.clearAllTimers();
		this.deps.log("info", "deferring daemon management", { reason });
		this.setState("defer");
		this.scheduleDeferProbe();
	}
	scheduleDeferProbe() {
		this.schedule(this.opts.probeIntervalMs, () => {
			this.deferProbe();
		});
	}
	async deferProbe() {
		const ep = this.epoch;
		const info = await this.safePing();
		if (this.invalidated(ep) || this._state !== "defer") return;
		if (info) {
			this.enterAdopted(info);
			return;
		}
		this.scheduleDeferProbe();
	}
	enterHosting() {
		const ep = ++this.epoch;
		this.clearAllTimers();
		this.setState("hosting");
		let proc;
		try {
			proc = this.deps.spawnDaemon();
		} catch (error) {
			this.deps.log("error", "failed to spawn daemon", { error: describeError(error) });
			this.enterBackoff("spawn-error", null);
			return;
		}
		this.proc = proc;
		proc.exited.then((code) => {
			if (this.invalidated(ep)) return;
			this.proc = null;
			this.deps.log("warn", "hosted daemon exited", {
				code,
				state: this._state
			});
			this.enterBackoff("daemon-exit", code);
		}, (error) => {
			if (this.invalidated(ep)) return;
			this.proc = null;
			this.deps.log("warn", "hosted daemon exit watch failed", { error: describeError(error) });
			this.enterBackoff("daemon-exit", null);
		});
		this.schedule(this.opts.hostReadyTimeoutMs, () => {
			if (this.invalidated(ep)) return;
			this.deps.log("warn", "hosted daemon readiness timeout", { timeoutMs: this.opts.hostReadyTimeoutMs });
			this.disposeProc();
			this.enterBackoff("ready-timeout", null);
		});
		this.scheduleReadyPoll(ep);
	}
	scheduleReadyPoll(ep) {
		this.schedule(this.opts.hostReadyPingIntervalMs, () => {
			this.readyPoll(ep);
		});
	}
	async readyPoll(ep) {
		if (this.invalidated(ep)) return;
		const info = await this.safePing();
		if (this.invalidated(ep) || this._state !== "hosting") return;
		if (info) {
			this.enterHosted(info);
			return;
		}
		this.scheduleReadyPoll(ep);
	}
	enterHosted(info) {
		this.clearAllTimers();
		this._lastPing = info;
		this.hostFailures = 0;
		this.pingFailures = 0;
		this.deps.log("info", "hosted daemon ready", {
			pid: info.pid,
			version: info.version
		});
		this.setState("hosted");
	}
	enterBackoff(reason, code) {
		this.epoch += 1;
		this.clearAllTimers();
		this.hostFailures += 1;
		this.setState("backoff");
		if (this.hostFailures >= this.opts.backoffLimit) {
			this.deps.log("error", "hosting failure budget exhausted; giving up", {
				failures: this.hostFailures,
				reason
			});
			this.setState("failed");
			return;
		}
		const delayMs = Math.min(this.opts.backoffBaseMs * 2 ** (this.hostFailures - 1), this.opts.backoffCapMs);
		this.deps.log("warn", "hosting failed; backing off", {
			reason,
			code,
			failures: this.hostFailures,
			delayMs
		});
		this.schedule(delayMs, () => {
			this.runDetermination("probe");
		});
	}
	invalidated(ep) {
		return this.stopped || this.epoch !== ep;
	}
	async safePing() {
		try {
			return await this.deps.ping();
		} catch (error) {
			this.deps.log("debug", "ping threw; treating as unreachable", { error: describeError(error) });
			return null;
		}
	}
	/** Fire-and-forget terminate of the self-spawned process (readiness timeout path). */
	disposeProc() {
		const proc = this.proc;
		this.proc = null;
		if (!proc) return;
		proc.terminate().catch((error) => {
			this.deps.log("warn", "terminate failed", { error: describeError(error) });
		});
	}
	setState(next) {
		if (this._state === next) return;
		const previous = this._state;
		this._state = next;
		this.deps.log("debug", "supervisor state transition", {
			from: previous,
			to: next
		});
		for (const listener of [...this.listeners]) try {
			listener(next, previous);
		} catch (error) {
			this.deps.log("warn", "state listener threw", { error: describeError(error) });
		}
	}
	schedule(ms, fn) {
		const set = this.deps.setTimeout ?? defaultSetTimeout;
		let handle;
		handle = set(() => {
			this.timers.delete(handle);
			if (this.stopped) return;
			fn();
		}, ms);
		this.timers.add(handle);
		return handle;
	}
	clearAllTimers() {
		const clear = this.deps.clearTimeout ?? defaultClearTimeout;
		for (const handle of this.timers) clear(handle);
		this.timers.clear();
	}
};
//#endregion
//#region src/index.ts
const name = "agent-sidecar";
/** Required services; see the module doc for why `agents` is lazy instead. */
const inject = ["webServer", "subprocess"];
/** `SOCKET_NAME` in sidecar/daemon.py. */
const SOCKET_NAME = "daemon.sock";
/** `RUNTIME_ENV` / `LEGACY_RUNTIME_ENV` in sidecar/daemon.py. */
const RUNTIME_ENV = "AGENT_SIDECAR_RUNTIME_DIR";
const LEGACY_RUNTIME_ENV = "AGENT_SIDECAR_HOME";
/** SIGTERM → grace → SIGKILL window for a hosted daemon (design §4.a: 5s). */
const DAEMON_GRACE_MS = 5e3;
/** Whole-run bound for one `service status` detection probe. */
const DETECT_TIMEOUT_MS = 1e4;
/** SIGTERM → grace → SIGKILL window when the send-cli hard timeout kills. */
const SEND_CLI_GRACE_MS = 2e3;
/** Output cap for the detection probe (one sanitized message line). */
const DETECT_OUTPUT_BYTES = 4096;
/** Per-line clamp when forwarding daemon output into ctx.logger (S8). */
const LOG_LINE_LIMIT = 400;
/** Per-page `replay` limit forwarded to the daemon (its own cap is 1024). */
const REPLAY_PAGE_LIMIT = 512;
/** Page cap per fusion replay pull: bounds one timeline fan-out to ≤2048 events. */
const REPLAY_MAX_PAGES = 4;
/** Timeline entries pulled into one session-analysis summary. */
const ANALYSIS_TIMELINE_LIMIT = 120;
/** Sessions listed per project-analysis overview. */
const ANALYSIS_MAX_SESSIONS = 30;
/** Project groups listed in a cross-agent analysis overview. */
const ANALYSIS_MAX_GROUPS = 12;
/** Sessions listed per group in a cross-agent analysis overview. */
const ANALYSIS_CROSS_SESSIONS = 5;
/** Clamp on one line of untrusted text (titles, event text). */
const ANALYSIS_LINE_CLAMP = 200;
/** Clamp on the user question (placed at the head, so it survives truncation). */
const ANALYSIS_QUESTION_CLAMP = 2e3;
/**
* `service status` messages that mean "a LaunchAgent owns daemon liveness"
* (sidecar/launchd.py `_status`): exit 0 is `service is running (pid N)`;
* exit 1 covers `service is loaded but daemon is not running` and
* `service is degraded; ...` (both installed) as well as
* `service is unloaded...` (not installed). There is no `--json` face —
* the single sanitized message line IS the contract.
*/
const SERVICE_PRESENT = /^service is (?:running|loaded|degraded)/m;
/**
* Resolve the effective runtime directory the way sidecar/daemon.py
* `default_runtime_dir()` does: explicit config wins, then the
* AGENT_SIDECAR_RUNTIME_DIR / legacy AGENT_SIDECAR_HOME environment of the
* dsh host process, then `~/.agent_sidecar`.
*/
function resolveRuntimeDir(configured, env) {
	const raw = configured.trim() !== "" ? configured.trim() : (env[RUNTIME_ENV] ?? env[LEGACY_RUNTIME_ENV] ?? "").trim();
	if (raw === "") return join(homedir(), ".agent_sidecar");
	const expanded = raw === "~" ? homedir() : raw.startsWith("~/") ? join(homedir(), raw.slice(2)) : raw;
	return isAbsolute(expanded) ? expanded : resolve(expanded);
}
/** Flatten and clamp one line of untrusted text for an analysis summary. */
function clampAnalysisText(text, max = ANALYSIS_LINE_CLAMP) {
	const flat = text.replace(/\s+/g, " ").trim();
	return flat.length <= max ? flat : `${flat.slice(0, max)}…`;
}
/** Same trailing-slash normalization fusion uses for project group keys. */
function normalizeAnalysisProject(project) {
	if (project.length > 1 && project.endsWith("/")) {
		const stripped = project.replace(/\/+$/, "");
		return stripped === "" ? "/" : stripped;
	}
	return project;
}
/** One unified-session line in a project / cross-agent overview. */
function describeUnifiedSession(session) {
	const title = session.title !== "" ? clampAnalysisText(session.title) : "(untitled)";
	const live = session.live ? "|live" : "";
	const updated = new Date(session.lastActivityAt).toISOString();
	return `- [${session.agent}|${session.status}${live}] ${title} (updated ${updated})`;
}
/**
* Assemble the M1 host half.
*
* Teardown is order-sensitive, so the whole assembly lives in ONE
* `ctx.effect` disposer (design §4.a: "顺序敏感拆除放同一 disposer"):
* supervisor first (terminates a self-hosted daemon, never an adopted one),
* then the reconciler (closes the subscribe stream and timers), then
* `routes.dispose()` (ends SSE clients, unsubscribes), and the webServer
* route disposer last.
*
* @param ctx - plugin context handed by the cordis loader.
* @param config - schema-validated composition config (defaults filled).
*/
function apply(ctx, config) {
	const runtimeDir = resolveRuntimeDir(config.sidecar.runtimeDir, process.env);
	const socketPath = join(runtimeDir, SOCKET_NAME);
	const command = config.sidecar.command;
	/** Explicit redirect only when configured; the ambient env already flows. */
	const childEnv = config.sidecar.runtimeDir.trim() !== "" ? { [RUNTIME_ENV]: runtimeDir } : void 0;
	const log = (level, msg, meta) => {
		ctx.logger[level](meta === void 0 ? `agent-sidecar: ${msg}` : `agent-sidecar: ${msg} ${JSON.stringify(meta)}`);
	};
	/** Clamped per-line forwarding of daemon output (design §4.c, S8-safe). */
	const forwardLines = (stream, level) => {
		if (stream === void 0) return;
		stream.on("error", () => {});
		createInterface({ input: stream }).on("line", (line) => {
			const text = line.length > LOG_LINE_LIMIT ? `${line.slice(0, LOG_LINE_LIMIT)}…` : line;
			if (text.trim() !== "") ctx.logger[level](`agent-sidecar daemon: ${text}`);
		});
	};
	/** Spawn `<command> daemon run` as a supervised foreground child. */
	const spawnDaemon = () => {
		const handle = ctx.subprocess.spawn({
			argv: [
				...command,
				"daemon",
				"run"
			],
			cwd: homedir(),
			stdio: {
				stdin: "ignore",
				stdout: "pipe",
				stderr: "pipe"
			},
			graceMs: DAEMON_GRACE_MS,
			env: childEnv
		});
		forwardLines(handle.stdout, "debug");
		forwardLines(handle.stderr, "warn");
		return {
			exited: handle.done.then((outcome) => outcome.exitCode),
			terminate: async () => {
				handle.terminate();
				await handle.waitForExit();
			}
		};
	};
	/**
	* Read-only LaunchAgent detection: darwin-only, one bounded
	* `service status` run, parsed per {@link SERVICE_PRESENT}. Any failure
	* (non-zero control exit, timeout, unspawnable CLI) reads as "absent" —
	* the supervisor already treats detection errors that way.
	*/
	const detectLaunchAgent = async () => {
		if (process.platform !== "darwin") return false;
		const handle = ctx.subprocess.spawn({
			argv: [
				...command,
				"service",
				"status"
			],
			cwd: homedir(),
			stdio: {
				stdin: "ignore",
				stdout: { maxBytes: DETECT_OUTPUT_BYTES },
				stderr: { maxBytes: DETECT_OUTPUT_BYTES }
			},
			graceMs: 2e3,
			signal: AbortSignal.timeout(DETECT_TIMEOUT_MS),
			env: childEnv
		});
		const outcome = await handle.done;
		if (outcome.exitCode === 0) return true;
		if (outcome.exitCode !== 1) return false;
		const text = handle.collected.stdout?.readFrom(0).text ?? "";
		return SERVICE_PRESENT.test(text);
	};
	const store = new SessionStore();
	const client = new SidecarSocketClient({ socketPath });
	const replayFace = { replay: async ({ sessionId, afterSeq }) => {
		const events = [];
		let cursor = afterSeq ?? 0;
		for (let page = 0; page < REPLAY_MAX_PAGES; page += 1) {
			const result = await client.replay(sessionId, cursor, REPLAY_PAGE_LIMIT);
			events.push(...result.events);
			if (!result.truncated || result.lastSeq === null || result.lastSeq <= cursor) break;
			cursor = result.lastSeq;
		}
		return events;
	} };
	const getSessionQuery = () => {
		const getter = ctx.get;
		if (typeof getter !== "function") return null;
		const engine = getter.call(ctx, "sessionQuery");
		return engine === void 0 || engine === null ? null : engine;
	};
	const buildFusion = (dshEvents) => new FusionQuery({
		store,
		dshEvents,
		getSessionQuery,
		replay: replayFace
	});
	const fusionHolder = { current: buildFusion(null) };
	const fusion = {
		getUnifiedSessions: () => fusionHolder.current.getUnifiedSessions(),
		getSessionTimeline: (sessionId, opts) => fusionHolder.current.getSessionTimeline(sessionId, opts),
		getProjectGroups: (opts) => fusionHolder.current.getProjectGroups(opts),
		getLineage: (sessionId) => fusionHolder.current.getLineage(sessionId),
		searchSessions: (query, opts) => fusionHolder.current.searchSessions(query, opts),
		getCapabilities: () => fusionHolder.current.getCapabilities()
	};
	const reconciler = new Reconciler(client, {
		applySnapshot: (rows) => {
			store.applySnapshot(rows);
		},
		applyEvent: (ev) => {
			store.applyEvent(ev);
			fusionHolder.current.ingestSidecarEvent(ev);
		},
		setStreamHealth: (health) => {
			store.setStreamHealth(health);
		},
		hasWorkingSessions: () => store.hasWorkingSessions()
	}, {
		activeMs: config.stream.reconcileActiveMs,
		idleMs: config.stream.reconcileIdleMs
	});
	const supervisor = new DaemonSupervisor({
		ping: () => client.ping(),
		spawnDaemon,
		detectLaunchAgent,
		log
	}, {
		policy: config.daemon.policy,
		backoffLimit: config.daemon.backoffLimit
	});
	let effective = config;
	const guardOptions = { allowWriteActions: () => effective.inject.enabled };
	let liveAgents = null;
	const dshExecutor = createDshInjectExecutor({
		agents: {
			get: (sessionId) => liveAgents?.get(sessionId),
			resume: (options) => liveAgents === null ? Promise.reject(/* @__PURE__ */ new Error("dsh agents service is not available in this composition")) : liveAgents.resume(options)
		},
		log,
		pluginName: name
	});
	const spawnSendCli = (argv) => {
		const handle = ctx.subprocess.spawn({
			argv,
			cwd: homedir(),
			stdio: {
				stdin: "pipe",
				stdout: "pipe",
				stderr: "pipe"
			},
			graceMs: SEND_CLI_GRACE_MS,
			env: childEnv
		});
		handle.stdin?.on("error", () => {});
		return {
			stdin: {
				write: (chunk) => {
					handle.stdin?.write(chunk);
				},
				end: () => {
					handle.stdin?.end();
				}
			},
			onStdout: (listener) => {
				handle.stdout?.on("data", listener);
			},
			onStderr: (listener) => {
				handle.stderr?.on("data", listener);
			},
			exited: handle.done.then((outcome) => outcome.exitCode),
			kill: () => {
				handle.terminate();
			}
		};
	};
	const sendCliExecutor = createSendCliExecutor({
		spawn: spawnSendCli,
		log,
		opts: { command }
	});
	/** Live target re-check against the reconciled store (§4.f.5 prepare). */
	const verifyTarget = async (target) => {
		const view = store.getBoardState().sessions.find((s) => s.agent === target.agent && s.session_id === target.sessionId);
		if (view === void 0) return null;
		return {
			agent: view.agent,
			sessionId: view.session_id,
			status: view.status,
			title: view.title,
			project: view.project
		};
	};
	const injectGateway = new InjectGateway({
		executors: {
			dsh: dshExecutor,
			sendCli: sendCliExecutor
		},
		verifyTarget,
		allowWrite: () => effective.inject.enabled,
		log: (entry) => log(entry.ok ? "info" : "warn", `inject ${entry.phase}`, entry)
	});
	const liveAnalysisSessions = /* @__PURE__ */ new Set();
	/**
	* Resolve the provider/model the analysis agent runs on (A-1 fix: an
	* agent created without agentOptions has no model — `{{model}}` prompt
	* assembly and `buildRequest` both fail, yielding an empty summary).
	* Explicit `analysis.provider`+`analysis.model` config wins (both
	* non-empty, read live); otherwise the host's default model selection is
	* reused via `ctx.agentDefaultModel` — the same source dsh's own entry
	* points (headless/apiproxy) read. `null` = no model anywhere: routes
	* pre-reject `analysis.request` as `analysis_model_unconfigured`.
	*/
	const resolveAnalysisModel = () => {
		const provider = effective.analysis.provider.trim();
		const model = effective.analysis.model.trim();
		if (provider !== "" && model !== "") return {
			provider,
			model
		};
		const getter = ctx.get;
		if (typeof getter !== "function") return null;
		const service = getter.call(ctx, "agentDefaultModel");
		if (service === void 0 || service === null) return null;
		try {
			const selection = service.currentSelection();
			if (typeof selection?.provider === "string" && selection.provider !== "" && typeof selection.model === "string" && selection.model !== "") return {
				provider: selection.provider,
				model: selection.model
			};
		} catch {}
		return null;
	};
	const createAnalysisAgent = async (options) => {
		const agents = liveAgents;
		if (agents === null) throw new Error("dsh agents service is not available in this composition");
		const selection = resolveAnalysisModel();
		if (selection === null) throw new Error("no analysis model available: set analysis.provider/analysis.model or mount agentDefaultModel");
		const handle = await agents.create({
			...options,
			agentOptions: {
				provider: selection.provider,
				model: selection.model
			},
			meta: { cwd: process.cwd() }
		});
		const tracked = {
			agent: handle.agent,
			dispose: async () => {
				liveAnalysisSessions.delete(tracked);
				await handle.dispose();
			}
		};
		liveAnalysisSessions.add(tracked);
		return tracked;
	};
	const analysisEngine = new AnalysisEngine({
		createAgent: createAnalysisAgent,
		allowAnalysis: () => effective.analysis.enabled,
		log: (entry) => log(entry.errorCode !== void 0 ? "warn" : "info", `analysis ${entry.op}`, entry)
	});
	/**
	* Assemble the bounded AnalysisInput for one target from fusion data
	* (design §4.e.3: summaries come from the fused timelines/overviews).
	* `null` = target unknown to fusion → the routes answer 404. The user
	* question rides the HEAD of the text so it survives the engine's
	* tail truncation, and the session timeline lists NEWEST events first
	* for the same reason: when the engine's head-keep truncation bites,
	* it should shed the oldest — least informative — events (F5).
	*/
	const buildAnalysisInput = async (req) => {
		const questionLines = req.question !== void 0 && req.question.trim() !== "" ? [
			"[用户问题 / question]",
			clampAnalysisText(req.question, ANALYSIS_QUESTION_CLAMP),
			""
		] : [];
		if (req.targetKind === "session") {
			const targetId = req.targetId ?? "";
			const session = fusion.getUnifiedSessions().find((s) => s.sessionId === targetId) ?? null;
			if (session === null) return null;
			const page = await fusion.getSessionTimeline(targetId, { limit: ANALYSIS_TIMELINE_LIMIT });
			const sources = page.sources;
			const summaryText = [
				...questionLines,
				`[会话概览 / session] agent=${session.agent} status=${session.status} live=${session.live}`,
				`title: ${session.title !== "" ? clampAnalysisText(session.title) : "(untitled)"}`,
				`project: ${session.project}`,
				`last activity: ${new Date(session.lastActivityAt).toISOString()}`,
				"",
				`[时间线 / timeline,最新在前 / newest first] ${page.entries.length} events (sources: dshLive=${sources.dshLive} dshCold=${sources.dshCold} replay=${sources.sidecarReplay} buffer=${sources.sidecarBuffer})`,
				...[...page.entries].reverse().map((entry) => `- [${new Date(entry.ts).toISOString()}] ${entry.kind}${entry.seq !== null ? ` seq=${entry.seq}` : ""}${entry.text !== "" ? ` ${clampAnalysisText(entry.text)}` : ""}`)
			].join("\n");
			return {
				kind: "session",
				title: session.title !== "" ? session.title : `${session.agent} ${session.sessionId}`,
				summaryText,
				meta: {
					targetId,
					agent: session.agent
				}
			};
		}
		if (req.targetKind === "project") {
			const wanted = normalizeAnalysisProject(req.targetId ?? "");
			const group = fusion.getProjectGroups().find((g) => normalizeAnalysisProject(g.project) === wanted) ?? null;
			if (group === null) return null;
			const omitted = group.sessions.length - ANALYSIS_MAX_SESSIONS;
			const summaryText = [
				...questionLines,
				`[项目概览 / project] ${group.project}`,
				`agents: ${group.agents.join(", ")} | sessions: ${group.sessions.length} | last activity: ${new Date(group.lastActivityAt).toISOString()}`,
				"",
				...group.sessions.slice(0, ANALYSIS_MAX_SESSIONS).map(describeUnifiedSession),
				...omitted > 0 ? [`… ${omitted} more sessions omitted`] : []
			].join("\n");
			return {
				kind: "project",
				title: `project ${group.project}`,
				summaryText,
				meta: { targetId: group.project }
			};
		}
		const groups = fusion.getProjectGroups();
		const sessionsTotal = groups.reduce((n, g) => n + g.sessions.length, 0);
		const omittedGroups = groups.length - ANALYSIS_MAX_GROUPS;
		return {
			kind: "cross-agent",
			title: "cross-agent overview",
			summaryText: [
				...questionLines,
				`[跨 agent 概览 / cross-agent overview] ${groups.length} projects, ${sessionsTotal} sessions in the correlation window`,
				"",
				...groups.slice(0, ANALYSIS_MAX_GROUPS).flatMap((group) => [
					`[${group.project}] agents: ${group.agents.join(", ")} | sessions: ${group.sessions.length}`,
					...group.sessions.slice(0, ANALYSIS_CROSS_SESSIONS).map(describeUnifiedSession),
					""
				]),
				...omittedGroups > 0 ? [`… ${omittedGroups} more projects omitted`] : []
			].join("\n")
		};
	};
	const routes = createRoutes({
		store,
		supervisor,
		guardOptions,
		injectGateway,
		fusion,
		analysisEnabled: () => effective.analysis.enabled,
		analysis: {
			engine: analysisEngine,
			buildInput: buildAnalysisInput,
			available: () => liveAgents !== null,
			modelConfigured: () => resolveAnalysisModel() !== null
		},
		log
	});
	ctx.effect(() => {
		const removeRoute = ctx.webServer.register({
			kind: "prefix",
			path: API_PREFIX,
			handler: routes.handle
		});
		const offStateChange = supervisor.onStateChange((state) => {
			if (state === "adopted" || state === "hosted") reconciler.reconcileNow();
		});
		fusionHolder.current.start();
		reconciler.start();
		supervisor.start();
		return async () => {
			offStateChange();
			await supervisor.stop();
			reconciler.stop();
			await Promise.all([...liveAnalysisSessions].map((handle) => handle.dispose().catch(() => {})));
			routes.dispose();
			removeRoute();
			fusionHolder.current.stop();
		};
	}, "agent-sidecar: host assembly (route + reconciler + supervisor + fusion + analysis)");
	ctx.inject(["sessions"], (injected) => {
		const sctx = injected;
		const bus = sctx;
		const withFeed = buildFusion({ on: (event, handler) => bus.on(event, handler) });
		withFeed.start();
		const previous = fusionHolder.current;
		fusionHolder.current = withFeed;
		previous.stop();
		sctx.effect(() => () => {
			const downgraded = buildFusion(null);
			downgraded.start();
			fusionHolder.current = downgraded;
			withFeed.stop();
		}, "agent-sidecar: fusion dsh feed release");
		log("debug", "fusion dsh event feed online (sessions service bound)");
	});
	ctx.inject(["agents"], (injected) => {
		const actx = injected;
		liveAgents = actx.agents;
		actx.effect(() => () => {
			liveAgents = null;
		}, "agent-sidecar: agents binding release");
		log("debug", "dsh inject + analysis paths online (agents service bound)");
	});
	ctx.inject(["settings"], (injected) => {
		try {
			const sctx = injected;
			const scope = sctx.settings.register(name, Config, {
				base: config,
				applies: "live"
			});
			effective = scope.get();
			const unwatch = scope.watch((next) => {
				effective = next;
			});
			sctx.effect(() => () => {
				unwatch();
				effective = config;
			}, "agent-sidecar: settings scope release");
			log("debug", "settings namespace registered", { applies: "live" });
		} catch (err) {
			log("warn", `settings namespace registration failed: ${String(err)}`);
		}
	});
	ctx.logger.info(`agent-sidecar: host half assembled (policy=${config.daemon.policy}, socket=${socketPath}, route=${API_PREFIX})`);
}
//#endregion
export { Config, apply, inject, name };
