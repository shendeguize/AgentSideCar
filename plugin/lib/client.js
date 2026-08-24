window.__ModuleLoader__.load({
	id: "@shendeguize/dsh-agent-sidecar",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
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
		const defaultSetTimeout$1 = (fn, ms) => globalThis.setTimeout(fn, ms);
		const defaultClearTimeout$1 = (handle) => {
			globalThis.clearTimeout(handle);
		};
		const defaultCreateAbortController$1 = () => new AbortController();
		function resolveFetch(opts) {
			if (opts.fetch !== void 0) return opts.fetch;
			return globalThis.fetch;
		}
		async function request(path, init, opts) {
			const doFetch = resolveFetch(opts);
			const controller = (opts.createAbortController ?? defaultCreateAbortController$1)();
			const setT = opts.setTimeout ?? defaultSetTimeout$1;
			const clearT = opts.clearTimeout ?? defaultClearTimeout$1;
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
		/** `GET <prefix>/state` — full board snapshot. */
		async function fetchState(opts = {}) {
			return await request(`${API_PREFIX}/state`, { method: "GET" }, opts);
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
		const defaultSetTimeout = (fn, ms) => globalThis.setTimeout(fn, ms);
		const defaultClearTimeout = (handle) => {
			globalThis.clearTimeout(handle);
		};
		const defaultCreateAbortController = () => new AbortController();
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
				this.setT = opts.setTimeout ?? defaultSetTimeout;
				this.clearT = opts.clearTimeout ?? defaultClearTimeout;
				this.createController = opts.createAbortController ?? defaultCreateAbortController;
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
		//#region src/client/board/strings.ts
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
		const BOARD_STRINGS = {
			/** Session status badge labels (observed values, see hover disclaimer). */
			status: {
				working: "工作中",
				waiting: "等待中",
				idle: "空闲",
				dead: "已结束",
				unknown: "未知"
			},
			/** Badge attention markers; gap outranks stale (per-session > global). */
			attention: {
				gap: "事件缺口",
				stale: "可能滞后"
			},
			/** Daemon supervisor state labels for the top-bar badge. */
			daemon: {
				probe: "探测中",
				adopted: "已连接 · 领养",
				defer: "等待系统服务",
				reprobe: "重新探测中",
				hosting: "正在启动",
				hosted: "已连接 · 托管",
				backoff: "重启退避中",
				failed: "离线"
			},
			/** Stream health indicator labels. */
			stream: {
				ok: "实时流正常",
				degraded: "实时流重连中",
				unknown: "实时流未建立"
			},
			/** Top banner texts (design §5.3: degraded warning / FAILED red banner). */
			banner: {
				daemonFailed: "sidecar 离线:看板显示最后一次快照,数据不再更新",
				streamDegraded: "实时流重连中,数据可能滞后"
			},
			/** Empty-state guidance blocks. */
			empty: {
				daemonFailedTitle: "sidecar 已离线",
				daemonFailedHint: "daemon 连续启动失败已熔断。可在设置卡重试,或手动运行 agent-sidecar daemon start 后等待自动领养。",
				daemonDeferTitle: "等待系统服务拉起 daemon",
				daemonDeferHint: "检测到 LaunchAgent 托管,插件不会自行启动 daemon;服务拉起后看板会自动出数。",
				filteredTitle: "当前过滤条件下没有会话",
				filteredHint: "试试放宽时间窗,或打开「显示已结束」。",
				noSessionsTitle: "暂无被观测的会话",
				noSessionsHint: "本机 agent(claude / codex / cursor / dsh …)开始工作后会自动出现在这里。"
			},
			/** Top bar controls. */
			topbar: {
				title: "Sidecar 多 agent 看板",
				refresh: "刷新",
				refreshTitle: "手动拉取最新快照",
				showDead: "显示已结束",
				timeWindow: "时间窗"
			},
			/** Session card texts. */
			card: {
				noEvent: "暂无事件",
				untitled: "(无标题)",
				/** Design §5.3 / SKILL.md wording: statuses are inferred observations. */
				observedDisclaimer: "状态为从持久化数据推断的观察值,可能滞后",
				observedValue: "观察值: {status}",
				lastReconcile: "最近对账: {time}",
				neverReconciled: "尚未对账"
			},
			/** Relative time templates. */
			time: {
				justNow: "刚刚",
				minutesAgo: "{n} 分钟前",
				hoursAgo: "{n} 小时前",
				daysAgo: "{n} 天前"
			},
			/** Time-window option templates. */
			timeWindow: {
				hours: "{n} 小时",
				days: "{n} 天"
			},
			/** Group header session count. */
			groupCount: "{n} 个会话",
			/** Sessions with an empty `project` fall into this group (task spec). */
			unknownProject: "未知项目",
			/** Footer widget. */
			widget: {
				label: "Sidecar",
				connection: {
					ok: "已连接",
					degraded: "连接不稳定",
					off: "离线"
				},
				working: "{n} 个会话工作中"
			}
		};
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
		const MINUTE_MS = 6e4;
		const HOUR_MS = 36e5;
		const DAY_MS = 864e5;
		/** Resolve `{name}` placeholders in a message template. */
		function formatTemplate(template, params) {
			return template.replace(/\{(\w+)\}/g, (match, key) => {
				const value = params[key];
				return value === void 0 ? match : String(value);
			});
		}
		const KNOWN_STATUSES = [
			"working",
			"waiting",
			"idle",
			"dead"
		];
		/** Map a raw observed status onto the badge vocabulary ('unknown' fallback). */
		function normalizeStatus(raw) {
			const cleaned = raw.trim().toLowerCase();
			return KNOWN_STATUSES.includes(cleaned) ? cleaned : "unknown";
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
		const STATUS_TONE = {
			working: "success",
			waiting: "warn",
			idle: "neutral",
			unknown: "neutral",
			dead: "muted"
		};
		/**
		* Visibility rules (task spec):
		* - dead sessions are hidden unless `showDead`;
		* - working sessions are always visible (even outside the window);
		* - everything else hides once `updatedAtMs` falls strictly beyond the
		*   window (age === window is still visible);
		* - a non-finite or non-positive window disables the age filter entirely.
		*/
		function isSessionVisible(session, filters, nowMs) {
			const status = normalizeStatus(session.status);
			if (status === "dead" && !filters.showDead) return false;
			if (status === "working") return true;
			const windowMs = filters.timeWindowHours * HOUR_MS;
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
		* status + gap + streamHealth → badge tone/label/attention.
		*
		* Priority: a per-session `gap` marker (a known data hole for THIS session)
		* outranks the global stale marker (stream reconnecting affects everyone
		* and is already surfaced by the top banner). Unknown raw statuses keep
		* their raw text as the label — the board never invents a state.
		*/
		function deriveBadge(rawStatus, gap, streamHealth) {
			const status = normalizeStatus(rawStatus);
			const trimmed = rawStatus.trim();
			const label = status === "unknown" ? trimmed === "" ? BOARD_STRINGS.status.unknown : trimmed : BOARD_STRINGS.status[status];
			let attention = null;
			if (gap) attention = "gap";
			else if (streamHealth !== "ok") attention = "stale";
			return {
				status,
				tone: STATUS_TONE[status],
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
			const reconcile = lastReconcileAtMs === null ? BOARD_STRINGS.card.neverReconciled : formatTemplate(BOARD_STRINGS.card.lastReconcile, { time: formatRelativeTime(lastReconcileAtMs, nowMs) });
			return [
				BOARD_STRINGS.card.observedDisclaimer,
				formatTemplate(BOARD_STRINGS.card.observedValue, { status: observed }),
				reconcile
			].join("\n");
		}
		/**
		* Coarse relative time: <60s (including clock skew into the future) is
		* 刚刚, then whole minutes/hours/days. Non-finite input renders empty.
		*/
		function formatRelativeTime(thenMs, nowMs) {
			if (!Number.isFinite(thenMs)) return "";
			const delta = nowMs - thenMs;
			if (delta < MINUTE_MS) return BOARD_STRINGS.time.justNow;
			if (delta < HOUR_MS) return formatTemplate(BOARD_STRINGS.time.minutesAgo, { n: Math.floor(delta / MINUTE_MS) });
			if (delta < DAY_MS) return formatTemplate(BOARD_STRINGS.time.hoursAgo, { n: Math.floor(delta / HOUR_MS) });
			return formatTemplate(BOARD_STRINGS.time.daysAgo, { n: Math.floor(delta / DAY_MS) });
		}
		/** Label for a time-window option: whole days as 天, otherwise 小时. */
		function timeWindowLabel(hours) {
			if (hours >= 24 && hours % 24 === 0) return formatTemplate(BOARD_STRINGS.timeWindow.days, { n: hours / 24 });
			return formatTemplate(BOARD_STRINGS.timeWindow.hours, { n: hours });
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
		/** Single-character agent marker; unknown agents get a neutral dot. */
		function agentGlyph(agent) {
			return AGENT_GLYPHS[agent.trim().toLowerCase()] ?? "●";
		}
		/** Head…tail abbreviation for long session ids (full id goes in `title`). */
		function abbreviateSessionId(id, max = 20) {
			if (id.length <= max) return id;
			return `${id.slice(0, 12)}…${id.slice(-6)}`;
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
		/** Count of sessions currently observed as working. */
		function countWorking(sessions) {
			let count = 0;
			for (const session of sessions) if (normalizeStatus(session.status) === "working") count += 1;
			return count;
		}
		/** Widget hover/aria text: connection state, plus the count when nonzero. */
		function widgetTitle(connection, workingCount) {
			const base = `${BOARD_STRINGS.widget.label}: ${BOARD_STRINGS.widget.connection[connection]}`;
			if (workingCount <= 0) return base;
			return `${base} · ${formatTemplate(BOARD_STRINGS.widget.working, { n: workingCount })}`;
		}
		/** filter → group → per-card derive; the one call Board.tsx renders from. */
		function buildBoardViewModel(input) {
			const { sessions, filters, daemonState, streamHealth, lastReconcileAtMs, nowMs } = input;
			const visible = filterSessions(sessions, filters, nowMs);
			return {
				groups: groupSessions(visible.map((session) => ({
					...session,
					badge: deriveBadge(session.status, session.gap, streamHealth),
					glyph: agentGlyph(session.agent),
					shortId: abbreviateSessionId(session.sessionId),
					relativeTime: formatRelativeTime(session.updatedAtMs, nowMs),
					hoverTitle: badgeHoverTitle(session.status, lastReconcileAtMs, nowMs)
				}))),
				banner: deriveBanner(daemonState, streamHealth),
				emptyState: deriveEmptyState(daemonState, visible.length, sessions.length),
				daemonBadge: deriveDaemonBadge(daemonState),
				streamLabel: BOARD_STRINGS.stream[streamHealth],
				streamTone: streamHealthTone(streamHealth),
				visibleCount: visible.length,
				totalCount: sessions.length,
				workingCount: countWorking(sessions)
			};
		}
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
		/** Parse + validate persisted filters; anything malformed reads as absent. */
		function readStoredFilters(storage) {
			if (storage === null) return null;
			try {
				const raw = storage.getItem(FILTERS_STORAGE_KEY);
				if (raw === null) return null;
				const parsed = JSON.parse(raw);
				if (typeof parsed !== "object" || parsed === null) return null;
				const candidate = parsed;
				if (typeof candidate.timeWindowHours !== "number" || !Number.isFinite(candidate.timeWindowHours) || candidate.timeWindowHours <= 0 || typeof candidate.showDead !== "boolean") return null;
				return {
					timeWindowHours: candidate.timeWindowHours,
					showDead: candidate.showDead
				};
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
			/** Manual refresh (board's refresh button): one out-of-band snapshot pull. */
			async refresh() {
				try {
					const snapshot = await this.fetchFn({});
					this.applySnapshot(snapshot);
				} catch (err) {
					console.error("agent-sidecar: manual refresh failed", err);
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
		//#region \0dsh-css:/Users/jingyu/Workspace/Projects/agent_sidecar/plugin/src/client/board/board.module.css.mjs
		const css$1 = ".aJ0YNW_root{box-sizing:border-box;height:100%;min-height:420px;color:var(--dsw-alias-label-primary,#1f2328);background:var(--dsw-alias-bg-base,transparent);flex-direction:column;gap:12px;padding:16px 20px;display:flex;overflow-y:auto}.aJ0YNW_topbar{flex-wrap:wrap;align-items:center;gap:10px;display:flex}.aJ0YNW_title{color:var(--dsw-alias-label-primary,#1f2328);margin-right:2px;font-size:15px;font-weight:650}.aJ0YNW_badge{border:1px solid var(--dsw-alias-border-l1,#00000014);background:var(--dsw-alias-bg-layer-1,#00000008);color:var(--dsw-alias-label-secondary,#57606a);white-space:nowrap;border-radius:999px;align-items:center;gap:5px;padding:0 8px;font-size:12px;line-height:20px;display:inline-flex}.aJ0YNW_dot{background:var(--dsw-alias-label-tertiary,#6e7781);border-radius:50%;flex:none;width:8px;height:8px}.aJ0YNW_dot[data-tone=success]{background:var(--dsw-alias-state-success-primary,#1a7f37)}.aJ0YNW_dot[data-tone=warn]{background:var(--dsw-alias-state-warn-primary,#9a6700)}.aJ0YNW_dot[data-tone=danger]{background:var(--dsw-alias-state-error-primary,#cf222e)}.aJ0YNW_dot[data-tone=neutral]{background:var(--dsw-alias-label-tertiary,#6e7781)}.aJ0YNW_dot[data-tone=muted]{background:var(--dsw-alias-label-dimmed,#8c959f)}.aJ0YNW_spacer{flex:1}.aJ0YNW_control{color:var(--dsw-alias-label-secondary,#57606a);white-space:nowrap;align-items:center;gap:5px;font-size:12px;display:inline-flex}.aJ0YNW_select{color:var(--dsw-alias-label-primary,#1f2328);background:var(--dsw-alias-bg-layer-1,transparent);border:1px solid var(--dsw-alias-border-l2,#0000001f);border-radius:6px;padding:2px 6px;font-family:inherit;font-size:12px}.aJ0YNW_checkbox{accent-color:var(--dsw-alias-brand-primary,#4d6bfe);margin:0}.aJ0YNW_refresh{color:var(--dsw-alias-label-secondary,#57606a);border:1px solid var(--dsw-alias-border-l1,#00000014);cursor:pointer;background:0 0;border-radius:6px;padding:2px 10px;font-family:inherit;font-size:12px}.aJ0YNW_refresh:hover{color:var(--dsw-alias-label-primary,#1f2328);border-color:var(--dsw-alias-border-l2,#0000001f);background:var(--dsw-alias-interactive-bg-hover,#0000000a)}.aJ0YNW_banner{border:1px solid var(--dsw-alias-border-l1,#00000014);border-radius:8px;padding:6px 10px;font-size:12px;line-height:18px}.aJ0YNW_banner[data-tone=warn]{color:var(--dsw-alias-state-warn-label,var(--dsw-alias-state-warn-primary,#9a6700));background:var(--dsw-alias-state-warn-tertiary,#9a670014);border-color:var(--dsw-alias-state-warn-secondary,#9a67003d)}.aJ0YNW_banner[data-tone=danger]{color:var(--dsw-alias-state-error-primary,#cf222e);background:var(--dsw-alias-state-error-secondary,#cf222e14);border-color:var(--dsw-alias-state-error-secondary,#cf222e3d)}.aJ0YNW_empty{text-align:center;flex-direction:column;flex:1;justify-content:center;align-items:center;gap:6px;min-height:180px;padding:24px;display:flex}.aJ0YNW_emptyTitle{color:var(--dsw-alias-label-primary,#1f2328);font-size:14px;font-weight:600}.aJ0YNW_emptyHint{max-width:420px;color:var(--dsw-alias-label-secondary,#57606a);font-size:12px;line-height:20px}.aJ0YNW_group{flex-direction:column;gap:8px;display:flex}.aJ0YNW_groupHead{align-items:baseline;gap:8px;min-width:0;display:flex}.aJ0YNW_groupName{color:var(--dsw-alias-label-primary,#1f2328);text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600;overflow:hidden}.aJ0YNW_groupCount{color:var(--dsw-alias-label-secondary,#57606a);background:var(--dsw-alias-bg-layer-2,#0000000a);border-radius:999px;flex:none;padding:0 8px;font-size:11px;line-height:18px}.aJ0YNW_grid{grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;display:grid}.aJ0YNW_card{text-align:left;border:1px solid var(--dsw-alias-border-l1,#00000014);background:var(--dsw-alias-bg-layer-1,#00000005);cursor:pointer;border-radius:8px;flex-direction:column;gap:6px;min-width:0;padding:10px 12px;font-family:inherit;display:flex}.aJ0YNW_card:hover{border-color:var(--dsw-alias-border-l2,#0000001f);background:var(--dsw-alias-interactive-bg-hover,#0000000a)}.aJ0YNW_card:focus-visible{outline:2px solid var(--dsw-alias-brand-primary,#4d6bfe);outline-offset:1px}.aJ0YNW_cardHead{justify-content:space-between;align-items:center;gap:8px;min-width:0;display:flex}.aJ0YNW_agent{text-overflow:ellipsis;white-space:nowrap;min-width:0;color:var(--dsw-alias-label-primary,#1f2328);align-items:center;gap:5px;font-size:12px;font-weight:600;display:inline-flex;overflow:hidden}.aJ0YNW_glyph{color:var(--dsw-alias-brand-primary,#4d6bfe);flex:none}.aJ0YNW_attention{color:var(--dsw-alias-state-warn-primary,#9a6700);font-size:11px}.aJ0YNW_attention[data-kind=gap]{color:var(--dsw-alias-state-error-primary,#cf222e)}.aJ0YNW_cardTitle{color:var(--dsw-alias-label-primary,#1f2328);text-overflow:ellipsis;white-space:nowrap;font-size:13px;overflow:hidden}.aJ0YNW_cardId{color:var(--dsw-alias-label-tertiary,#6e7781);text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;overflow:hidden}.aJ0YNW_cardEvent{color:var(--dsw-alias-label-secondary,#57606a);text-overflow:ellipsis;white-space:nowrap;font-size:12px;overflow:hidden}.aJ0YNW_cardTime{color:var(--dsw-alias-label-caption,var(--dsw-alias-label-tertiary,#6e7781));font-size:11px}";
		const tagId$1 = "@shendeguize/dsh-agent-sidecar/board.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId$1) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
			tag.dataset.pluginCss = tagId$1;
			tag.textContent = css$1;
			document.head.appendChild(tag);
		}
		var board_module_css_default = {
			"agent": "aJ0YNW_agent",
			"attention": "aJ0YNW_attention",
			"badge": "aJ0YNW_badge",
			"banner": "aJ0YNW_banner",
			"card": "aJ0YNW_card",
			"cardEvent": "aJ0YNW_cardEvent",
			"cardHead": "aJ0YNW_cardHead",
			"cardId": "aJ0YNW_cardId",
			"cardTime": "aJ0YNW_cardTime",
			"cardTitle": "aJ0YNW_cardTitle",
			"checkbox": "aJ0YNW_checkbox",
			"control": "aJ0YNW_control",
			"dot": "aJ0YNW_dot",
			"empty": "aJ0YNW_empty",
			"emptyHint": "aJ0YNW_emptyHint",
			"emptyTitle": "aJ0YNW_emptyTitle",
			"glyph": "aJ0YNW_glyph",
			"grid": "aJ0YNW_grid",
			"group": "aJ0YNW_group",
			"groupCount": "aJ0YNW_groupCount",
			"groupHead": "aJ0YNW_groupHead",
			"groupName": "aJ0YNW_groupName",
			"refresh": "aJ0YNW_refresh",
			"root": "aJ0YNW_root",
			"select": "aJ0YNW_select",
			"spacer": "aJ0YNW_spacer",
			"title": "aJ0YNW_title",
			"topbar": "aJ0YNW_topbar"
		};
		//#endregion
		//#region src/client/board/Board.tsx
		/** Time-window choices offered by the top bar (hours). */
		const TIME_WINDOW_OPTIONS = [
			6,
			12,
			24,
			48,
			168
		];
		function SessionCard(props) {
			const { card, onSelect } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
				type: "button",
				className: board_module_css_default["card"],
				onClick: () => onSelect(card.sessionId),
				"data-testid": "agent-sidecar-card",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: board_module_css_default["cardHead"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: board_module_css_default["agent"],
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: board_module_css_default["glyph"],
								"aria-hidden": true,
								children: card.glyph
							}), card.agent]
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: board_module_css_default["badge"],
							"data-tone": card.badge.tone,
							title: card.hoverTitle,
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["dot"],
									"data-tone": card.badge.tone
								}),
								card.badge.label,
								card.badge.attention !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["attention"],
									"data-kind": card.badge.attention,
									children: card.badge.attentionLabel
								})
							]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: board_module_css_default["cardTitle"],
						title: card.title,
						children: card.title.trim() === "" ? BOARD_STRINGS.card.untitled : card.title
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: board_module_css_default["cardId"],
						title: card.sessionId,
						children: card.shortId
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
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: board_module_css_default["group"],
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: board_module_css_default["groupHead"],
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: board_module_css_default["groupName"],
						title: group.fullPath === "" ? void 0 : group.fullPath,
						children: group.label
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: board_module_css_default["groupCount"],
						children: formatTemplate(BOARD_STRINGS.groupCount, { n: group.cards.length })
					})]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: board_module_css_default["grid"],
					children: group.cards.map((card) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SessionCard, {
						card,
						onSelect
					}, `${card.agent}:${card.sessionId}`))
				})]
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
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: board_module_css_default["root"],
				"data-testid": "agent-sidecar-board",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("header", {
						className: board_module_css_default["topbar"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: board_module_css_default["title"],
								children: BOARD_STRINGS.topbar.title
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
								className: board_module_css_default["badge"],
								"data-tone": vm.daemonBadge.tone,
								title: props.daemonDetail,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["dot"],
									"data-tone": vm.daemonBadge.tone
								}), vm.daemonBadge.label]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
								className: board_module_css_default["badge"],
								"data-tone": vm.streamTone,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["dot"],
									"data-tone": vm.streamTone
								}), vm.streamLabel]
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
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: board_module_css_default["refresh"],
								title: BOARD_STRINGS.topbar.refreshTitle,
								onClick: props.onRefresh,
								children: BOARD_STRINGS.topbar.refresh
							})
						]
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
		//#region src/client/widget.tsx
		const DOT_COLORS = {
			ok: "var(--dsw-alias-state-success-primary, #1a7f37)",
			degraded: "var(--dsw-alias-state-warn-primary, #9a6700)",
			off: "var(--dsw-alias-label-dimmed, #8c959f)"
		};
		const rootStyle = {
			display: "inline-flex",
			alignItems: "center",
			gap: 4,
			padding: "2px 6px",
			border: "none",
			borderRadius: 6,
			background: "transparent",
			cursor: "pointer",
			font: "inherit",
			color: "var(--dsw-alias-label-secondary, #57606a)"
		};
		const countStyle = {
			fontSize: 11,
			fontVariantNumeric: "tabular-nums",
			lineHeight: "14px",
			whiteSpace: "nowrap"
		};
		/** Connection dot + working-session counter (e.g. `▸2`) for the footer. */
		function SidecarWidget(props) {
			const title = widgetTitle(props.connection, props.workingCount);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
				type: "button",
				style: rootStyle,
				title,
				"aria-label": title,
				onClick: props.onOpen,
				"data-testid": "agent-sidecar-widget",
				"data-connection": props.connection,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
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
				})]
			});
		}
		/** Complete shipped dictionaries keyed by locale id. */
		const dictionaries = {
			zh: {
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
				"settings.injectEnabledHint": "关闭时看板隐藏全部注入入口,写接口在服务端同步拒绝。",
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
				"settings.skillProvideHint": "经 registerProvider 向 dsh 提供 agent-sidecar skill(M4 启用;重启后生效)。"
			},
			en: {
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
				"settings.injectEnabledHint": "When off, the board hides every inject affordance and the server rejects write actions.",
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
				"settings.skillProvideHint": "Provide the agent-sidecar skill to dsh via registerProvider (enabled in M4; applies after restart)."
			}
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
		//#region \0dsh-css:/Users/jingyu/Workspace/Projects/agent_sidecar/plugin/src/client/settings-card.module.css.mjs
		const css = ".TiEuyW_card{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-3);border-radius:12px;list-style:none;transition:border-color .16s,background .16s}.TiEuyW_card:hover{border-color:var(--dsw-alias-label-dimmed)}.TiEuyW_cardOpen{background:var(--dsw-alias-bg-layer-2);border-color:var(--dsw-alias-label-dimmed)}.TiEuyW_header{appearance:none;width:100%;font:inherit;color:inherit;text-align:left;cursor:pointer;background:0 0;border:0;border-radius:12px;align-items:center;gap:12px;padding:14px 16px;display:flex}.TiEuyW_header:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:-2px}.TiEuyW_headText{flex-direction:column;flex:1;gap:4px;min-width:0;display:flex}.TiEuyW_name{color:var(--dsw-alias-label-primary);font-size:15px;font-weight:600;line-height:1.4}.TiEuyW_description{color:var(--dsw-alias-label-tertiary);font-size:13px;line-height:1.5}.TiEuyW_pending{white-space:nowrap;background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary);border-radius:999px;flex:none;padding:1px 8px;font-size:11px;font-weight:500;line-height:17px}.TiEuyW_chevron{border-right:1.5px solid var(--dsw-alias-label-tertiary);border-bottom:1.5px solid var(--dsw-alias-label-tertiary);flex:none;width:8px;height:8px;transition:transform .16s;transform:rotate(45deg)translateY(-2px)}.TiEuyW_chevronOpen{transform:rotate(225deg)translateY(-2px)}.TiEuyW_body{border-top:1px solid var(--dsw-alias-border-l2);margin:0 16px;padding-bottom:8px}.TiEuyW_readOnly{color:var(--dsw-alias-label-tertiary);margin:12px 0 0;font-size:12px;line-height:1.5}.TiEuyW_section{padding:12px 0 4px}.TiEuyW_section+.TiEuyW_section{border-top:1px solid var(--dsw-alias-border-l2)}.TiEuyW_sectionTitle{color:var(--dsw-alias-label-primary);margin:0 0 4px;font-size:13px;font-weight:600;line-height:1.5}.TiEuyW_field{flex-direction:column;gap:6px;padding:8px 0;display:flex}.TiEuyW_label{color:var(--dsw-alias-label-primary);font-size:13px;line-height:1.5}.TiEuyW_hint{color:var(--dsw-alias-label-tertiary);margin:0;font-size:12px;line-height:1.5}.TiEuyW_invalidHint{color:var(--dsw-alias-label-error);margin:0;font-size:12px;line-height:1.5}.TiEuyW_input,.TiEuyW_select{appearance:none;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-3);max-width:420px;font:inherit;color:var(--dsw-alias-label-primary);border-radius:8px;padding:6px 10px;font-size:13px;line-height:1.5}.TiEuyW_input:focus-visible,.TiEuyW_select:focus-visible{border-color:var(--dsw-alias-brand-primary);outline:none}.TiEuyW_input::placeholder{color:var(--dsw-alias-label-tertiary)}.TiEuyW_input:disabled,.TiEuyW_select:disabled{opacity:.5;cursor:default}.TiEuyW_inputInvalid{border-color:var(--dsw-alias-label-error)}.TiEuyW_toggleRow{cursor:pointer;align-items:center;gap:10px;display:flex}.TiEuyW_toggleRow input{accent-color:var(--dsw-alias-brand-primary);cursor:pointer;width:16px;height:16px;margin:0}.TiEuyW_toggleRow input:disabled{cursor:default}.TiEuyW_note{background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary);border-radius:8px;margin:6px 0 2px;padding:8px 10px;font-size:12px;line-height:1.6}.TiEuyW_statusRow{align-items:center;gap:8px;padding:4px 0;display:flex}.TiEuyW_statusDot{background:var(--dsw-alias-label-tertiary);border-radius:50%;flex:none;width:8px;height:8px}.TiEuyW_statusOk{background:var(--dsw-alias-brand-primary)}.TiEuyW_statusError{background:var(--dsw-alias-label-error)}.TiEuyW_statusText{color:var(--dsw-alias-label-primary);font-size:13px;line-height:1.5}.TiEuyW_statusMeta{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:1.5}.TiEuyW_retry{appearance:none;border:1px solid var(--dsw-alias-border-l2);font:inherit;color:var(--dsw-alias-label-secondary);cursor:pointer;background:0 0;border-radius:8px;padding:3px 12px;font-size:12px;line-height:1.5}.TiEuyW_retry:hover:not(:disabled){color:var(--dsw-alias-label-primary);border-color:var(--dsw-alias-label-dimmed)}.TiEuyW_retry:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}.TiEuyW_footer{border-top:1px solid var(--dsw-alias-border-l2);align-items:center;gap:8px;padding:12px 0 4px;display:flex}.TiEuyW_docs{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:1.5;text-decoration:none}.TiEuyW_docs:hover{color:var(--dsw-alias-label-primary);text-decoration:underline}.TiEuyW_failed{text-align:right;min-width:0;color:var(--dsw-alias-label-error);flex:1;margin:0;font-size:12px;line-height:1.5}.TiEuyW_spacer{flex:1}.TiEuyW_discard,.TiEuyW_save{appearance:none;font:inherit;cursor:pointer;border:1px solid #0000;border-radius:8px;padding:5px 14px;font-size:13px;line-height:1.5}.TiEuyW_discard{border-color:var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary);background:0 0}.TiEuyW_discard:hover:not(:disabled){color:var(--dsw-alias-label-primary);border-color:var(--dsw-alias-label-dimmed)}.TiEuyW_save{background:var(--dsw-alias-label-primary);color:var(--dsw-alias-bg-layer-3)}.TiEuyW_discard:disabled,.TiEuyW_save:disabled{opacity:.4;cursor:default}.TiEuyW_discard:focus-visible,.TiEuyW_save:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}";
		const tagId = "@shendeguize/dsh-agent-sidecar/settings-card.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
			tag.dataset.pluginCss = tagId;
			tag.textContent = css;
			document.head.appendChild(tag);
		}
		var settings_card_module_css_default = {
			"body": "TiEuyW_body",
			"card": "TiEuyW_card",
			"cardOpen": "TiEuyW_cardOpen",
			"chevron": "TiEuyW_chevron",
			"chevronOpen": "TiEuyW_chevronOpen",
			"description": "TiEuyW_description",
			"discard": "TiEuyW_discard",
			"docs": "TiEuyW_docs",
			"failed": "TiEuyW_failed",
			"field": "TiEuyW_field",
			"footer": "TiEuyW_footer",
			"headText": "TiEuyW_headText",
			"header": "TiEuyW_header",
			"hint": "TiEuyW_hint",
			"input": "TiEuyW_input",
			"inputInvalid": "TiEuyW_inputInvalid",
			"invalidHint": "TiEuyW_invalidHint",
			"label": "TiEuyW_label",
			"name": "TiEuyW_name",
			"note": "TiEuyW_note",
			"pending": "TiEuyW_pending",
			"readOnly": "TiEuyW_readOnly",
			"retry": "TiEuyW_retry",
			"save": "TiEuyW_save",
			"section": "TiEuyW_section",
			"sectionTitle": "TiEuyW_sectionTitle",
			"select": "TiEuyW_select",
			"spacer": "TiEuyW_spacer",
			"statusDot": "TiEuyW_statusDot",
			"statusError": "TiEuyW_statusError",
			"statusMeta": "TiEuyW_statusMeta",
			"statusOk": "TiEuyW_statusOk",
			"statusRow": "TiEuyW_statusRow",
			"statusText": "TiEuyW_statusText",
			"toggleRow": "TiEuyW_toggleRow"
		};
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
		/** Text field with a local draft so typing survives a non-echoing beat. */
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
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
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
		/**
		* Integer field with a local draft: invalid intermediate text (empty,
		* non-numeric, below the schema minimum) shows the invalid hint and commits
		* nothing, so the staged value can never leave the schema's domain.
		*/
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
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						id,
						className: invalid ? `${settings_card_module_css_default["input"]} ${settings_card_module_css_default["inputInvalid"]}` : settings_card_module_css_default["input"],
						type: "text",
						inputMode: "numeric",
						value: draft,
						disabled: props.disabled,
						...invalid ? { "aria-invalid": true } : {},
						onChange: (event) => {
							const text = event.target.value;
							setDraft(text);
							const parsed = Number(text);
							const acceptable = text.trim() !== "" && Number.isInteger(parsed) && parsed >= props.min;
							setInvalid(!acceptable);
							if (acceptable && parsed !== props.value) props.onCommit(parsed);
						}
					}),
					invalid ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: settings_card_module_css_default["invalidHint"],
						role: "alert",
						children: props.invalidHint
					}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: settings_card_module_css_default["hint"],
						children: props.hint
					})
				]
			});
		}
		/**
		* Render the Agent Sidecar settings card.
		* @param props - staged values, form state, and the wiring callbacks.
		* @returns the card.
		*/
		function SettingsCard(props) {
			const [open, setOpen] = (0, react.useState)(false);
			const t$1 = props.t ?? t;
			const { values } = props;
			const disabled = !props.writable || props.saving;
			const title = t$1("settings.cardTitle");
			const daemonNote = props.daemon?.state === "defer" ? t$1("settings.daemonDeferNote") : props.daemon?.state === "failed" ? t$1("settings.daemonFailedNote") : void 0;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("li", {
				className: `${settings_card_module_css_default["card"]} ${open ? settings_card_module_css_default["cardOpen"] : ""}`,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: settings_card_module_css_default["header"],
					"aria-expanded": open,
					"aria-label": `${t$1(open ? "settings.collapse" : "settings.expand")}: ${title}`,
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
								children: t$1("settings.cardDescription")
							})]
						}),
						props.dirty ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: settings_card_module_css_default["pending"],
							children: t$1("settings.unsaved")
						}) : null,
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: `${settings_card_module_css_default["chevron"]} ${open ? settings_card_module_css_default["chevronOpen"] : ""}`,
							"aria-hidden": true
						})
					]
				}), open ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: settings_card_module_css_default["body"],
					children: [
						!props.writable ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
							className: settings_card_module_css_default["readOnly"],
							role: "status",
							children: t$1("settings.readOnly")
						}) : null,
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$1("settings.sectionDaemon"),
							children: [
								props.daemon ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: settings_card_module_css_default["field"],
									children: [
										/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: settings_card_module_css_default["label"],
											children: t$1("settings.daemonStatusLabel")
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
													children: t$1(DAEMON_STATE_KEY[props.daemon.state])
												}),
												props.daemon.pid !== void 0 && props.daemon.version !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: settings_card_module_css_default["statusMeta"],
													children: t$1("settings.daemonPidVersion", {
														pid: props.daemon.pid,
														version: props.daemon.version
													})
												}) : null,
												props.daemon.state === "failed" && props.onDaemonRetry !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
													type: "button",
													className: settings_card_module_css_default["retry"],
													onClick: props.onDaemonRetry,
													children: t$1("settings.daemonRetry")
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
									label: t$1("settings.daemonPolicyLabel"),
									hint: t$1("settings.daemonPolicyHint"),
									value: values.daemonPolicy,
									disabled,
									options: [
										{
											value: "adopt-or-host",
											label: t$1("settings.daemonPolicyAdoptOrHost")
										},
										{
											value: "adopt-only",
											label: t$1("settings.daemonPolicyAdoptOnly")
										},
										{
											value: "off",
											label: t$1("settings.daemonPolicyOff")
										}
									],
									onCommit: (value) => {
										props.onChange("daemonPolicy", value);
									}
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
									label: t$1("settings.daemonBackoffLimitLabel"),
									hint: t$1("settings.daemonBackoffLimitHint"),
									invalidHint: t$1("settings.invalidNumber", { min: 1 }),
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
							title: t$1("settings.sectionSidecar"),
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(TextField, {
								label: t$1("settings.sidecarCommandLabel"),
								hint: t$1("settings.sidecarCommandHint"),
								value: values.sidecarCommand,
								placeholder: "agent-sidecar",
								disabled,
								onCommit: (value) => {
									props.onChange("sidecarCommand", value);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(TextField, {
								label: t$1("settings.sidecarRuntimeDirLabel"),
								hint: t$1("settings.sidecarRuntimeDirHint"),
								value: values.sidecarRuntimeDir,
								placeholder: "~/.agent_sidecar",
								disabled,
								onCommit: (value) => {
									props.onChange("sidecarRuntimeDir", value);
								}
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$1("settings.sectionStream"),
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
								label: t$1("settings.streamActiveMsLabel"),
								hint: t$1("settings.streamActiveMsHint"),
								invalidHint: t$1("settings.invalidNumber", { min: 100 }),
								min: 100,
								value: values.streamReconcileActiveMs,
								disabled,
								onCommit: (value) => {
									props.onChange("streamReconcileActiveMs", value);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
								label: t$1("settings.streamIdleMsLabel"),
								hint: t$1("settings.streamIdleMsHint"),
								invalidHint: t$1("settings.invalidNumber", { min: 100 }),
								min: 100,
								value: values.streamReconcileIdleMs,
								disabled,
								onCommit: (value) => {
									props.onChange("streamReconcileIdleMs", value);
								}
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$1("settings.sectionInject"),
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
									className: settings_card_module_css_default["note"],
									children: t$1("settings.injectSafetyNote")
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
									label: t$1("settings.injectEnabledLabel"),
									hint: t$1("settings.injectEnabledHint"),
									checked: values.injectEnabled,
									disabled,
									onCommit: (checked) => {
										props.onChange("injectEnabled", checked);
									}
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(SelectField, {
									label: t$1("settings.injectDefaultModeLabel"),
									hint: t$1("settings.injectDefaultModeHint"),
									value: values.injectDefaultMode,
									disabled,
									options: [{
										value: "queue",
										label: t$1("settings.injectModeQueue")
									}, {
										value: "steer",
										label: t$1("settings.injectModeSteer")
									}],
									onCommit: (value) => {
										props.onChange("injectDefaultMode", value);
									}
								})
							]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Section, {
							title: t$1("settings.sectionAnalysis"),
							children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
								label: t$1("settings.analysisEnabledLabel"),
								hint: t$1("settings.analysisEnabledHint"),
								checked: values.analysisEnabled,
								disabled,
								onCommit: (checked) => {
									props.onChange("analysisEnabled", checked);
								}
							})
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$1("settings.sectionUi"),
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
								label: t$1("settings.uiTimeWindowHoursLabel"),
								hint: t$1("settings.uiTimeWindowHoursHint"),
								invalidHint: t$1("settings.invalidNumber", { min: 1 }),
								min: 1,
								value: values.uiTimeWindowHours,
								disabled,
								onCommit: (value) => {
									props.onChange("uiTimeWindowHours", value);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
								label: t$1("settings.uiShowDeadLabel"),
								hint: t$1("settings.uiShowDeadHint"),
								checked: values.uiShowDead,
								disabled,
								onCommit: (checked) => {
									props.onChange("uiShowDead", checked);
								}
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Section, {
							title: t$1("settings.sectionSkill"),
							children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
								label: t$1("settings.skillProvideLabel"),
								hint: t$1("settings.skillProvideHint"),
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
									children: t$1("settings.docsLink")
								}) : null,
								props.saveFailed === true ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
									className: settings_card_module_css_default["failed"],
									role: "status",
									children: t$1("settings.saveFailed")
								}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: settings_card_module_css_default["spacer"] }),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: settings_card_module_css_default["discard"],
									disabled: !props.dirty || props.saving,
									onClick: props.onDiscard,
									children: t$1("settings.discard")
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: settings_card_module_css_default["save"],
									disabled: !props.dirty || props.saving || !props.writable,
									onClick: props.onSave,
									children: t$1(props.saving ? "settings.saving" : "settings.save")
								})
							]
						})
					]
				}) : null]
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
			analysis: { enabled: false },
			ui: {
				timeWindowHours: 24,
				showDead: false
			},
			skill: { provide: false }
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
				analysis: { enabled: values.analysisEnabled },
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
		* Slot-facing React glue (T2.4): binds the {@link SidecarController} stores
		* to the three presentational modules via `useSyncExternalStore` and hands
		* back zero-prop components ready for slot registration. Factories close
		* over the controller so subscribe/getSnapshot identities stay stable
		* across renders (uSES resubscribes on identity change).
		*
		* The settings card entry additionally owns the staged-edit lifecycle over
		* a bound `SettingsScope` (browser mirror of the host settings namespace):
		* resolved values come from the scope snapshot, edits stage locally, save
		* writes one complete top-level group per changed group (see
		* settings-glue.ts for the write-granularity rationale), and success is
		* judged by comparing the post-write snapshot against the staged target —
		* `scope.set` settles without rejecting even when the host declines the
		* write (it recovers by reloading host state instead).
		*/
		/** Cross-agent board bound to the controller (the "Sidecar" conversation tab). */
		function createBoardTab(controller) {
			const subscribe = (cb) => controller.subscribe(cb);
			const getState = () => controller.getState();
			const getFilters = () => controller.getFilters();
			return function SidecarBoardTab() {
				const state = (0, react.useSyncExternalStore)(subscribe, getState, getState);
				const filters = (0, react.useSyncExternalStore)(subscribe, getFilters, getFilters);
				return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Board, {
					daemonState: state.daemonState,
					...state.daemonDetail !== void 0 ? { daemonDetail: state.daemonDetail } : {},
					streamHealth: state.streamHealth,
					lastReconcileAtMs: state.lastReconcileAtMs,
					sessions: state.sessions,
					filters,
					onFiltersChange: (next) => {
						controller.setFilters(next);
					},
					onRefresh: () => {
						controller.refresh();
					},
					onSelectSession: () => {}
				});
			};
		}
		/** Footer connection dot + working counter bound to the controller. */
		function createFooterWidget(controller) {
			const subscribe = (cb) => controller.subscribe(cb);
			const getState = () => controller.getState();
			return function SidecarFooterWidget() {
				const state = (0, react.useSyncExternalStore)(subscribe, getState, getState);
				return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SidecarWidget, {
					connection: deriveWidgetConnection(state.daemonState, state.streamHealth),
					workingCount: countWorking(state.sessions)
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
		//#region src/client/index.ts
		const name = "agent-sidecar";
		/** The slot registry is the only hard dependency; settingsScope is lazy. */
		const inject = ["slots"];
		/** Entry id (list slots) and cell key (settings keyed slot) in one. */
		const ENTRY_ID = "agent-sidecar";
		/** Host-side settings namespace (host half registers it via ctx.settings). */
		const SETTINGS_NAMESPACE = "agent-sidecar";
		/**
		* Apply-guard: whether the slot ledger already holds this plugin's entry
		* (list slots match by `id`, the keyed settings slot by `key`; both use
		* the same 'agent-sidecar' token). `entries()` answers [] for undeclared
		* slots, and the check runs inside the deferred `slots.inject` callback —
		* i.e. at actual registration time, when the ledger is authoritative.
		*/
		function hasOwnEntry(ctx, slot) {
			return ctx.slots.entries(slot).some((entry) => entry.options.id === ENTRY_ID || entry.options.key === SETTINGS_NAMESPACE);
		}
		/**
		* Style text per data-plugin-css tag id, cached at module scope so it
		* survives unload → re-apply cycles of one materialized module (the CSS
		* factory body only runs once per materialization).
		*/
		const styleTextCache = /* @__PURE__ */ new Map();
		/**
		* Keeper effect for the tsdown-injected `<style data-plugin>` tags: cache
		* their text, restore any tag a previous unload removed, and remove all of
		* this plugin's tags on dispose.
		*/
		function keepStylesAlive() {
			if (typeof document === "undefined") return () => {};
			const ownTags = `style[data-plugin=${JSON.stringify(PLUGIN_ID)}]`;
			for (const el of Array.from(document.querySelectorAll(ownTags))) {
				const key = el.dataset["pluginCss"];
				if (key !== void 0) styleTextCache.set(key, el.textContent ?? "");
			}
			for (const [key, cssText] of styleTextCache) if (document.querySelector(`style[data-plugin-css=${JSON.stringify(key)}]`) === null) {
				const tag = document.createElement("style");
				tag.dataset["plugin"] = PLUGIN_ID;
				tag.dataset["pluginCss"] = key;
				tag.textContent = cssText;
				document.head.appendChild(tag);
			}
			return () => {
				for (const el of Array.from(document.querySelectorAll(ownTags))) el.remove();
			};
		}
		/**
		* Mount the board tab, footer widget, and settings card, and run the data
		* controller for as long as the plugin fiber lives.
		* @param ctx - browser plugin context handed by the client loader.
		*/
		function apply(ctx) {
			const controller = new SidecarController();
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
				const SidecarBoardTab = createBoardTab(controller);
				ctx.slots.inject("conversation.view", () => {
					if (hasOwnEntry(ctx, "conversation.view")) return () => {};
					try {
						return ctx.slots.register({
							name: "conversation.view",
							id: ENTRY_ID,
							order: 30,
							label: "Sidecar"
						}, SidecarBoardTab);
					} catch (err) {
						console.error("agent-sidecar: board tab registration failed", err);
						return () => {};
					}
				});
			} catch (err) {
				console.error("agent-sidecar: board tab mount failed", err);
			}
			try {
				const SidecarFooterWidget = createFooterWidget(controller);
				ctx.slots.inject("sidebar.footer.action", () => {
					if (hasOwnEntry(ctx, "sidebar.footer.action")) return () => {};
					try {
						return ctx.slots.register({
							name: "sidebar.footer.action",
							id: ENTRY_ID,
							order: 30
						}, SidecarFooterWidget);
					} catch (err) {
						console.error("agent-sidecar: footer widget registration failed", err);
						return () => {};
					}
				});
			} catch (err) {
				console.error("agent-sidecar: footer widget mount failed", err);
			}
			try {
				ctx.inject(["settingsScope"], (injected) => {
					const sctx = injected;
					try {
						const scope = sctx.settingsScope.bind({ namespace: SETTINGS_NAMESPACE });
						const adoptDefaults = () => {
							const ui = scope.getSnapshot().value?.ui;
							if (ui !== void 0) controller.adoptConfigDefaults(ui);
						};
						sctx.effect(() => scope.subscribe(adoptDefaults), "agent-sidecar: settings→filter defaults");
						adoptDefaults();
						const SidecarSettingsCardEntry = createSettingsCardEntry(controller, scope);
						sctx.slots.inject("settings.plugin.item", () => {
							if (hasOwnEntry(sctx, "settings.plugin.item")) return () => {};
							try {
								return sctx.slots.register({
									name: "settings.plugin.item",
									key: SETTINGS_NAMESPACE
								}, SidecarSettingsCardEntry);
							} catch (err) {
								console.error("agent-sidecar: settings card registration failed", err);
								return () => {};
							}
						});
					} catch (err) {
						console.error("agent-sidecar: settings card mount failed", err);
					}
				});
			} catch (err) {
				console.error("agent-sidecar: settings scope injection failed", err);
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