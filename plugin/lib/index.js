import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import z from "@deepseek-ai/schemastery";
import { createConnection } from "node:net";
import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
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
	analysis: z.object({ enabled: z.boolean().default(false).description("AI 旁路分析开关(M3;消耗模型 token,默认关闭)") }).description("旁路分析"),
	ui: z.object({
		timeWindowHours: z.natural().min(1).default(24).description("看板会话时间窗(小时)"),
		showDead: z.boolean().default(false).description("是否显示 dead 会话")
	}).description("看板界面"),
	skill: z.object({ provide: z.boolean().default(false).description("是否经 registerProvider 内嵌提供 agent-sidecar skill(M4 启用)") }).description("skill 模式")
});
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
		const message = String(error["message"] ?? code);
		return /* @__PURE__ */ new Error(`${code}: ${message}`);
	}
	return new Error(String(error ?? "daemon_error"));
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
	maxLineBytes;
	constructor(opts) {
		this.socketPath = opts.socketPath;
		this.timeoutMs = opts.timeoutMs ?? 1e3;
		this.maxLineBytes = opts.maxLineBytes ?? 33554432;
		if (this.timeoutMs <= 0 || this.maxLineBytes <= 0) throw new RangeError("client bounds are invalid");
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
	* Open a subscribe stream: write the op, validate the ack, then deliver
	* each JSONL event through `handlers.onEvent`. After the ack the
	* connection may idle indefinitely (no timeout), matching
	* sidecar/client.py which disables its socket timeout post-handshake.
	*/
	subscribe(handlers) {
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
			socket.write("{\"op\":\"subscribe\"}\n");
		});
		socket.on("data", (chunk) => lines.push(chunk));
		return { close: () => finish() };
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
	running = false;
	backoffMs;
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
		this.backoffMs = this.reconnectMinMs;
	}
	start() {
		if (this.running) return;
		this.running = true;
		this.backoffMs = this.reconnectMinMs;
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
	async reconcileNow() {
		if (!this.running) return;
		if (this.reconcileInFlight) {
			this.reconcileQueued = true;
			return;
		}
		this.reconcileInFlight = true;
		try {
			const snapshot = await this.client.status();
			if (this.running && snapshot !== null) this.store.applySnapshot(snapshot.sessions);
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
		const delay = this.store.hasWorkingSessions() ? this.activeMs : this.idleMs;
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
function describeError$2(error) {
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
					const detail = describeError$2(error);
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
				const detail = describeError$2(error);
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
	const handleSession = (res, rawId) => {
		let id;
		try {
			id = decodeURIComponent(rawId);
		} catch {
			writeJson(res, 404, { reason: "session_not_found" });
			return;
		}
		const view = id === "" ? void 0 : deps.store.getBoardState().sessions.find((s) => s.session_id === id);
		if (view === void 0) {
			writeJson(res, 404, { reason: "session_not_found" });
			return;
		}
		writeJson(res, 200, {
			session: view,
			timeline: null,
			timelineNote: "timeline_not_available_until_m3"
		});
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
		if (subpath.startsWith("session/")) {
			if (method !== "GET") return writeMethodNotAllowed(res, "GET");
			handleSession(res, subpath.slice(8));
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
	const reconciler = new Reconciler(client, store, {
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
	const routes = createRoutes({
		store,
		supervisor,
		guardOptions,
		injectGateway: new InjectGateway({
			executors: {
				dsh: dshExecutor,
				sendCli: sendCliExecutor
			},
			verifyTarget,
			allowWrite: () => effective.inject.enabled,
			log: (entry) => log(entry.ok ? "info" : "warn", `inject ${entry.phase}`, entry)
		}),
		log
	});
	ctx.effect(() => {
		const removeRoute = ctx.webServer.register({
			kind: "prefix",
			path: API_PREFIX,
			handler: routes.handle
		});
		reconciler.start();
		supervisor.start();
		return async () => {
			await supervisor.stop();
			reconciler.stop();
			routes.dispose();
			removeRoute();
		};
	}, "agent-sidecar: host assembly (route + reconciler + supervisor)");
	ctx.inject(["agents"], (injected) => {
		const actx = injected;
		liveAgents = actx.agents;
		actx.effect(() => () => {
			liveAgents = null;
		}, "agent-sidecar: agents binding release");
		log("debug", "dsh inject path online (agents service bound)");
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
