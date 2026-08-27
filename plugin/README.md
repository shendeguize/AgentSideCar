# @shendeguize/dsh-agent-sidecar

[Agent Sidecar](../README.md) 的原生 dsh 插件:在 dsh Web 的 Agent Center 中跨 agent 监控本机 AI agent 会话——看板、会话详情时间线、消息注入(默认关)、AI 旁路分析(默认关)、skill 内嵌提供,以及对 sidecar daemon 的自动托管。

## 能力(当前已交付,M1-M3 + skill/sidebar)

- **一等 Agent Center overlay**:通过 dsh 官方 `shell.overlay` 注册 `agent-sidecar-center`(`order: 30`),由可观察导航状态驱动官方 Modal 内的大尺寸中心。dsh 主侧栏入口、footer 状态小件与 `/sidecar` 的 Agent Center 动作共享此入口,空白会话和窄屏下也可打开;会话区「Sidecar」Tab 保留为第二入口。嵌套 Modal 会把每个下层 dialog 同时标为 `inert` 与 `aria-hidden`,逐层关闭或卸载时精确恢复此前的宿主属性和焦点。
- **跨 agent 会话看板**:Agent Center overlay 与会话区「Sidecar」Tab 展示本机受支持 agent(cursor IDE/CLI、claude、codex、copilot、dsh、kimi)的会话卡片与状态徽标(`working` / `waiting` / `idle` / `dead`;状态为从持久化数据推断的观察值,可能滞后),并可按 agent 类型过滤。首个快照前显示本地化 loading 且根节点为 `aria-busy`;首载失败结束 busy 并允许手动重试,后续刷新或流失败则保留最后成功快照并如实提示 stale/degraded 状态。
- **会话详情视图**:统一时间线(融合 sidecar 规范化事件与 dsh 进程内实时事件,分页回溯历史,事件缺口如实标注,并公开不含正文的来源结果/降级状态);dsh 会话专属谱系树与全文检索(经 `sessionQuery`,该服务缺席时优雅降级——检索退化为标题/项目过滤,谱系显示不可用提示);项目分组视图(同一项目下跨 agent 会话并排呈现)。从看板/项目进入详情时焦点移入详情控件;返回时按 agent+session 组合身份恢复精确来源及各视图独立的有界滚动位置,来源消失或被过滤时回退到视图标题。详情内跳转到另一会话会清除原来源身份,返回时同样走标题 fallback。
- **消息注入(默认关)**:`inject.enabled` 是独立全局门,会话是否可注入另由 host 按目标派生。普通外部 agent 仅允许本机顶层 `claude`/`codex`/`cursor-cli` 的 waiting/idle 会话;Kimi Code 仅允许经 0.38.0 ACP 受保护 spawn-resume 的本机顶层 waiting/idle 会话。两者都经 `agent-sidecar send <full-session-id> --agent <agent> --exact-session --message-stdin --allow-write --json` 执行,消息不进入 Sidecar argv,Kimi 提示词也只进入 ACP NDJSON 而不进入 Kimi argv。本机 DSH working 可走进程内 steer,waiting/idle 进入 live/cold 预检。unsupported/remote/dead/invalid 目标置灰并显示可访问原因。`delivery: unknown` 不提供重试按钮。
- **AI 旁路分析(默认关)**:`analysis.enabled` 开启后,可对被观测会话/项目拉起专用 dsh 分析会话(有界摘要注入 + 增量追问 + 随时停止);模型路由见配置表 `analysis.provider` / `analysis.model`。分析正文绝不写入插件日志。
- **footer 状态小件**:侧边栏底部常驻连接状态点(sidecar 连接态 + 速览),点击打开 Agent Center。
- **`/sidecar` 斜杠命令**:会话输入框内的只读状态速览(daemon 状态、连接健康度、working/waiting 计数、按项目分组的活跃会话);选择末尾的「Agent Center」动作可打开 Agent Center。
- **设置卡**:dsh 设置页出现「Agent Sidecar」卡片(设置命名空间 `agent-sidecar`)。`analysis.provider` / `analysis.model` 以成对路由呈现:双空表示宿主默认,双非空表示显式路由,partial pair 不能保存;非 live 的 daemon/sidecar/stream 组只显示 profile patch + 重启说明。
- **daemon 托管**:探测-领养-否则托管(probe-adopt-else-host)策略管理 sidecar daemon 生命周期,详见[下文](#daemon-托管策略)。
- **实时数据面**:host 半区经 Unix socket 消费 daemon(`status` 快照对账 + `subscribe` 事件触发),浏览器经同源 SSE 实时刷新。
- **skill 内嵌提供**:`skill.provide`(默认开)经 dsh skill 注册表提供 agent-sidecar skill,装插件即得、无需运行安装脚本;文件系统已安装的同名 skill 自动优先(dsh 注册表按名合并,文件系统层胜出,无需探测)。
- **better-sidebar 可选摘要 Tab**(软依赖):装有 `dsh-better-sidebar` 时注册紧凑「Sidecar」侧边 Tab(连接点 + 计数 + 最近会话摘要);未装静默跳过;Tab 不可见时释放订阅,不额外轮询、不另建 SSE。
- **跟随宿主 locale 的中英双语界面**:向 dsh locale 服务注册中英文词典,采用宿主当前语言并跟随 `locale/change`;仅在宿主 locale 服务缺席时回退到插件内置中文,不会在正常 dsh Web 中形成默认中文孤岛。

## 前置条件

- **dsh ≥ 0.1.1-rc.2**(`package.json` 的 `engines.dsh` / `dsh.engines.dsh` 均为 `>=0.1.1-rc.2`)。
- peer 依赖 `@deepseek-ai/cordis@^4.0.1` 与 `@deepseek-ai/dsh-agent@^0.1.1-rc.2` 均为 optional,由 dsh profile 树提供,无需手工安装。
- **agent-sidecar CLI(可选)**:daemon 托管(自动拉起)与 macOS LaunchAgent 检测需要本机可调用的 `agent-sidecar`(或经 `sidecar.command` 配置的任意调用形态,如 `["python3", "/path/to/agent-sidecar.pyz"]`)。插件**绝不代装** sidecar;安装方法见[主仓 README](../README.md#installation)。
- 不装 CLI 也能用:只要已有 daemon 在跑(仓库 checkout 手动拉起、LaunchAgent 等),插件经 Unix socket 探测后**只读领养**它,不掌握其生死。
- Node/pnpm 仅开发本插件时需要;npm 包携带预构建产物,安装免构建。

## 安装

`dsh plugin add` 委托 pnpm 解析,故支持 pnpm 的三种包来源(将 `web` 换成你的 profile 名即可):

```sh
# ① npm 包名
dsh plugin --profile web add @shendeguize/dsh-agent-sidecar

# ② git 来源(monorepo 子目录,须带 path 选择器)
dsh plugin --profile web add github:shendeguize/AgentSideCar#path:plugin

# ③ 本地路径(仓库 checkout)
dsh plugin --profile web add /path/to/agent_sidecar/plugin
```

注意事项:

- **registry 镜像**:若本机默认 npm registry 是镜像(如 npmmirror),`@deepseek-ai` / `@shendeguize` scope 可能滞后或 dist-tag 陈旧,建议把这两个 scope 指向官方 registry(本目录内 `.npmrc` 即此写法)。
- **profile 用法**:对不存在的 profile,`dsh plugin --profile <name> add …` 会先初始化它;自定义 profile 需在其 manifest 的 bundles 中包含 `@deepseek-ai/dsh-web-app` 才有 Web 界面(`dsh web` 等价于 `dsh --profile web`)。启动:`dsh --profile <name> [--port N]`。
- **卸载**:`dsh plugin --profile <name> remove @shendeguize/dsh-agent-sidecar`,幂等、可重装。

装好后的主要入口与表面:

1. dsh 主侧栏 → 一等「Agent Center」入口,打开官方 Modal 承载的 Agent Center overlay;
2. 侧边栏 footer → 可点击的状态小件,打开同一 overlay;
3. 会话输入框 `/sidecar` → 只读摘要及打开同一 overlay 的「Agent Center」动作;
4. 会话页顶部 Tab 环 → 保留的「Sidecar」第二入口;
5. 设置页插件区 →「Agent Sidecar」设置卡。

安装 `dsh-better-sidebar` 后还会出现可选「Sidecar」摘要 Tab;它是软依赖,不影响以上原生入口。

命令行验证:`curl http://127.0.0.1:<port>/plugins/agent-sidecar/api/state` 应返回含 `daemon` / `board` / `capabilities` 的 JSON 快照;`GET …/api/stream` 为 SSE 流;`GET …/api/session/<id>` 为单会话详情(含融合时间线首页),`GET …/api/session/<id>/timeline?cursor=&limit=` 分页回溯历史;`GET …/api/lineage/<id>`、`GET …/api/search?q=`、`GET …/api/projects` 为 M3 读面;`POST …/api/action` 为幂等动作信封(`inject.prepare` / `inject.execute` / `analysis.*` / `daemon.retry`)。

## 时间线公共合同

- **规范条目与排序**:host 融合 DSH live 事件、按次晚绑定的 `sessionQuery`、sidecar replay 与有界事件缓冲。带 seq 的条目按 `seq + kind + text` 去重,因此同一原生 seq 的多个内容块不会被误删;无 seq 条目按 `ts + kind + text` 去重。seq 域严格按 seq 排序,同 seq 内 DSH 条目在前、兄弟块保留稳定顺序,再与无 seq 条目按时间交错。
- **分页收敛**:同 seq 兄弟组不会跨分页边界拆分。重叠页按同一身份去重;后续页面若为既有 DSH 条目补到规范文本,client 会升级空文本条目而不是复制一条。`nextCursor` 只随历史分页前移;listen 的最新窗口不会重置历史游标。
- **来源与降级**:每页除兼容性的 `sources` 外,还返回不含内容的 `sourceOutcomes`(`liveSession` / `sessionQuery` / `sidecarReplay` / `buffer`,取值为 `succeeded` / `unavailable` / `not_found` / `replay_unsupported` / `source_failed`)以及 `degraded`、`reason`。部分来源失败时保留现有/可用事件并显示降级提示;全部可用来源失败且没有新条目时明确提示「未能加载新事件」并给出刷新入口。未知或不一致的新字段关闭失败为「来源状态无法确认」,不会把上游错误文本、路径或会话内容带到 UI。
- **晚绑定与请求代际**:`sessionQuery` 每次使用时重新解析,后挂载的服务无需重载 client 即可生效。详情元数据与时间线各自有代际;新的最新窗口刷新会取消并取代旧分页/listen/刷新任务,晚到响应不能回滚条目、来源健康、header 或游标。已显示的 partial 警告只会被新代际中经过验证的健康页清除。

## 注入资格矩阵

资格判定由 host 在仍持有完整只读会话行时派生,浏览器只接收 `{allowed, reason}`;原始 `extra`、远端标记、parent ID 与私有路径不跨该边界。

- `inject.enabled=false`:全局门关闭,所有写动作服务端 403;这是独立于下列单会话资格的总开关。开启它不会把不合格会话变为合格。
- `claude` / `codex` / `cursor-cli`:仅本机、顶层、`waiting` 或 `idle` 合格。`working`、child/sidechain、remote、dead 与结构无效行拒绝。
- `kimi`:仅本机、顶层、`waiting` 或 `idle` 合格,执行器只接受精确验证的 Kimi Code 0.38.0 ACP。`working`、child/sidechain、remote、dead 与结构无效行拒绝。
- `dsh`:本机 `working` 可走 live Agent 的进程内 steer;本机 `waiting` / `idle` 进入 DSH live/cold preflight,其中 live 复用现有 Agent,cold 才尝试持久化恢复。DSH child/sidechain 拓扑由该原生 preflight 判定,不套用外部 Agent 的静态 child 拒绝。
- `cursor-ide` / `copilot` 及未知 Agent:没有注入执行器,入口置灰。

禁用按钮和面板都会通过本地化文案与 `aria-describedby` 暴露原因;缺失、过期或身份不匹配的 verdict 按 `invalid_session` 关闭失败,client 不根据 agent/status 自行猜测放行。

## Kimi 0.38.0 受保护恢复契约

Kimi 通路是**受保护 spawn-resume**,不是对现有终端的 followup 或 steer:

- 插件只支持精确版本 Kimi Code 0.38.0。每次执行启动独立的 `kimi acp` 进程,通过 ACP `session/resume` 恢复已持久化会话;不附着、不控制现有 Kimi 终端。旧版 Agent Sidecar 仍可能返回兼容错误 `unsupported_kimi`;这表示已安装的 Sidecar 过旧或版本验证未通过,不表示当前插件合同把 Kimi 整体列为不支持。
- UI 内部固定提交 `queue`,但用户界面只称「受保护恢复」并明确标注 non-steer;不会把它描述为下一轮排队或中途转向。
- 插件调用 Sidecar 时使用 `--message-stdin`;Sidecar 再把消息写入 ACP JSON-RPC NDJSON 的 `session/prompt` 帧。消息不进入 `agent-sidecar` argv,也不进入 Kimi 子进程 argv。
- ACP 恢复后固定进入 default/manual mode。所有 permission 或 question 请求一律回复 `cancelled`,绝不自动批准权限、回答问题或绕过交互门。
- 只有本机、顶层、`waiting` / `idle` 会话可执行;`working`、child/sidechain、remote、dead 一律在提示词写入前拒绝。
- 即使 Kimi turn 正常 completed,目前仍无法证明提示词已持久投递到恢复会话,所以结果保持 `delivery: "unknown"`。同一内容不得自动或手工盲目重试。使用同一 `request_id` 重放是安全的:Sidecar 返回已缓存回执且不再 spawn ACP 进程、不重复发送内容。

## DSH 注入的 live/cold 契约

DSH 进程内注入在 prepare 阶段先区分目标是否已经加载:

- **live Agent**:`ctx.agents.get(sessionId)` 命中时,插件直接复用该 Agent 既有的模型路由与 preset,不解析、不覆盖其配置,也不触发 cold resume 的路由/preset 限制。queue/steer 分别调用该实例的 `followup` / `steer`。
- **cold session**:未命中 live Agent 时,插件才通过宿主 `agentDefaultModel.currentSelection()` 读取**当前默认** provider/model,并在恢复时把这个完整 pair 作为 `agentOptions`。pair 缺失、空白或不完整时,`inject.prepare` 返回 HTTP 409:

  ```json
  {"reason":"dsh_model_unconfigured"}
  ```

  此时不会创建 requestId/confirmToken,不会调用 resume,也不消耗模型 token。
- **陈旧看板与权威持久化**:看板行只是观察快照。若 cold 检查时权威 `sessionPersistence.list` 已证明目标不存在,prepare 返回 HTTP 404 `{"reason":"target_not_found"}`,即使卡片仍在也不会尝试 resume 或签发 token。
- **cold preset 当前不受支持**:插件检查持久化 header 与最新有效 `agent-preset/selected` 事件得到 effective preset;只要目标 session 的持久化状态证明 preset 存在,prepare 就返回 HTTP 409 `{"reason":"dsh_preset_unsupported"}`。目标 session 没有显式 preset 时,宿主是否挂载 `agentPresets` 不会改变判定,因此 preset-free cold resume 可以继续走 host default model 路由。本文档不承诺 preset cold resume;该限制不影响上述 live Agent 路径。
- **未知状态关闭失败**:权威持久化/preset 服务缺席、读取失败、返回不合法形状、检查超时或无法证明 missing/present/absent 时,prepare 以 HTTP 502 拒绝且不签发 token;无 agents binding 时错误码为 `dsh_agents_unavailable`。prepare 与 execute 之间若发生服务 HMR/代际切换,或恢复发布前的实际会话/preset 与检查结果不一致,执行也会关闭失败并撤销未发布的恢复。响应可包含有界、脱敏的 `detail`,并只给稳定错误码;绝不输出 provider/model、preset 值、消息正文或私有持久化路径。

`inject.execute` 返回 `{"outcome":"delivered"}` 时,对 DSH 通路只表示同步
`followup`/`steer` 调用已经把消息交给目标 inbox(接受或排队)。它**不表示**模型 turn
已经开始或成功,也不保证工具执行结果;请继续观察详情 timeline 的新事件,或直接查看
目标终端 turn。`unknown` 仍是禁止重试的终态。

以上 DSH 能力全部来自插件进程内通路。直接运行 `agent-sidecar send` 指向 DSH
会话仍返回 `unsupported_dsh`;这只表示 Sidecar CLI 没有 DSH stock headless
resume,不得据此声称 DSH 注入整体不受支持。

外部 Agent 通路总是使用 `--agent <agent> --exact-session`,把完整会话 ID 与精确 Agent
组成复合目标,不允许退回前缀或跨 Agent 命中。插件还会校验 JSON 回执中的 Agent、
完整 session ID 与 request ID;任一不匹配都按投递未知的 `executor_error` 处理,
绝不展示为成功或自动重试。

## 配置

配置走双轨:

- **组合配置**(profile 的 `cordis.patch.yml` 中对 `id: agent-sidecar` 行加 `config:` 块):schemastery 校验,所有字段带默认值,零配置即可挂载。层叠对 `config` 是**整块替换**而非深度合并,覆写时请写全所需字段组。
- **设置卡**(dsh 设置页,命名空间 `agent-sidecar`):可视化暂存并持久化 live 的 inject/analysis/ui 组;daemon/sidecar/stream 当前只读展示,直接说明应修改 profile patch 并重启 DSH。

组合配置示例:

```yaml
- id: agent-sidecar
  config:
    daemon:
      policy: adopt-only
```

### 配置全表(与 `src/config.ts` 一致)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `daemon.policy` | `'adopt-or-host'` \| `'adopt-only'` \| `'off'` | `adopt-or-host` | daemon 托管策略:`adopt-or-host` 探测并领养既有 daemon,否则自行拉起;`adopt-only` 只领养绝不拉起;`off` 不管理生命周期(仍只读对账既有 daemon 的数据) |
| `daemon.backoffLimit` | 自然数(≥1) | `5` | 托管失败熔断阈值:连续失败达到该次数后停止重启并进入 FAILED |
| `sidecar.command` | `string[]` | `['agent-sidecar']` | sidecar 可执行命令(argv 前缀):PATH 名、绝对路径,或多段命令(如 `["python3", "/path/to/agent-sidecar.pyz"]`);插件绝不代装 |
| `sidecar.runtimeDir` | `string` | `''` | 运行时目录:留空按 daemon 同款规则解析(`AGENT_SIDECAR_RUNTIME_DIR` 环境变量,兼容旧名 `AGENT_SIDECAR_HOME`,否则 `~/.agent_sidecar`);非空时经环境变量传给受托管的 daemon |
| `stream.reconcileActiveMs` | 自然数(≥100) | `2000` | 对账快照周期(有会话工作中,毫秒) |
| `stream.reconcileIdleMs` | 自然数(≥100) | `10000` | 对账快照周期(空闲,毫秒) |
| **`inject.enabled`** | `boolean` | **`false`(默认关)** | **注入总开关**:关闭时看板隐藏全部注入入口,写接口在服务端同步拒绝(403)。多用户主机不建议开启 |
| `inject.defaultMode` | `'queue'` \| `'steer'` | `queue` | 注入面板默认模式:`queue` 排队下一轮,`steer` 中途注入 |
| `analysis.enabled` | `boolean` | `false` | AI 旁路分析开关(消耗模型 token,默认关闭) |
| `analysis.provider` | `string` | `''` | 分析代理 provider。与 `analysis.model` 构成 pair:双空时复用宿主默认模型(`agentDefaultModel` 服务,与 dsh 自身入口同源),双非空时使用显式路由;设置卡会标出 partial pair 并阻止保存 |
| `analysis.model` | `string` | `''` | 分析代理模型 id。pair 规则同上;双空且宿主无默认模型时,`analysis.request` 被拒为 `analysis_model_unconfigured` |
| `ui.timeWindowHours` | 自然数(≥1) | `24` | 看板会话时间窗(小时) |
| `ui.showDead` | `boolean` | `false` | 是否显示 dead 会话 |
| `skill.provide` | `boolean` | **`true`(默认开)** | 是否经 registerProvider 内嵌提供 agent-sidecar skill;文件系统已安装的同名 skill 自动优先;装配时读取,改动需重载插件生效 |

### 生效方式(如实说明)

- `inject.enabled`:**即时生效**(host 侧实时读取,守卫立即响应)。
- `analysis.*`:**即时生效**(开关与 provider/model 均按次实时读取)。
- `ui.*`:**即时生效**(浏览器侧实时读取,作为看板筛选默认值;用户手动筛选后以用户选择为准)。
- `skill.provide`:**装配时读取**(重启语义)——改动需重载插件生效。
- `daemon.*` / `stream.*` / `sidecar.*`:当前**以组合配置(patch 的 `config:` 块)为准**,在插件装配时烘焙定型;设置卡对这三组只显示只读说明,不提供会被误解为即时生效的编辑控件。要改这三组,请改 profile patch 后重启 DSH。

<a id="theming"></a>

## Theming / 自定义风格

插件公开三层样式契约,既跟随 dsh Skin Center,又允许皮肤或其他插件做有界覆盖:

1. **L1——宿主主题继承**:所有插件级颜色与阴影默认值都委托给 dsh 的 `--dsw-*` 语义 token,字体代码栈委托给 `--ds-font-family-code`。因此 dsh 主题或 Skin Center 改变这些 token 时,Agent Sidecar 会自动跟随,无需维护平行色板。
2. **L2——稳定 surface selector**:使用 `[data-dsh-plugin='agent-sidecar'][data-dsh-part='...']` 定位一个公开 surface。当前公开 part 以 `src/client/theme/parts.ts` 为唯一事实来源:
   - `board`:会话看板根;`board-toolbar`:看板工具栏;`board-card`:会话卡片。
   - `project-view`:项目分组视图;`detail`:会话详情根;`timeline`:统一时间线。
   - `inject-panel`:注入面板;`analysis-panel`:AI 分析面板;`dsh-tools`:dsh 谱系/搜索工具。
   - `footer-widget`:footer 状态入口;`sidebar-entry`:主侧栏 Agent Center 入口;`sidebar-tab`:可选 better-sidebar 摘要。
   - `settings-card`:设置卡;`overlay`:Agent Center overlay 根,由官方 `shell.overlay` Modal consumer 实际渲染。
3. **L3——`--agsc-*` 覆盖变量**:变量可设在全部插件 surface 上,也可设在某个 L2 selector 上做局部覆盖。默认语义如下:

| 变量 | 默认值 | 语义 |
|---|---|---|
| `--agsc-accent` | `var(--dsw-alias-brand-primary)` | 品牌强调、选中态与焦点 |
| `--agsc-bg` | `var(--dsw-alias-bg-layer-1)` | 基础 surface 背景 |
| `--agsc-bg-raised` | `var(--dsw-alias-bg-layer-3)` | 卡片、输入等抬升背景 |
| `--agsc-fg` | `var(--dsw-alias-label-primary)` | 主文字 |
| `--agsc-fg-secondary` | `var(--dsw-alias-label-secondary)` | 次级文字 |
| `--agsc-fg-dimmed` | `var(--dsw-alias-label-dimmed)` | 弱化文字与关闭态 |
| `--agsc-border` | `var(--dsw-alias-border-l1)` | 普通边界 |
| `--agsc-border-strong` | `var(--dsw-alias-border-l2)` | 强调边界 |
| `--agsc-ok` | `var(--dsw-alias-state-success-primary)` | 成功/健康状态 |
| `--agsc-warn` | `var(--dsw-alias-state-warn-primary)` | 警告/降级状态 |
| `--agsc-err` | `var(--dsw-alias-state-error-primary)` | 错误状态 |
| `--agsc-radius-card` | `12px` | 卡片与大容器圆角 |
| `--agsc-radius-control` | `8px` | 按钮、输入与小控件圆角 |
| `--agsc-shadow-card` | `var(--dsw-shadow-lv2)` | 卡片阴影 |
| `--agsc-font-mono` | `var(--ds-font-family-code)` | 会话 ID、代码与等宽内容 |

最小覆盖示例:

```css
/* 全局调整 Agent Sidecar 卡片圆角。 */
[data-dsh-plugin='agent-sidecar'] {
  --agsc-radius-card: 16px;
}

/* 只让看板 surface 使用较浅的宿主背景层。 */
[data-dsh-plugin='agent-sidecar'][data-dsh-part='board'] {
  --agsc-bg-raised: var(--dsw-alias-bg-layer-2);
}
```

**兼容性契约**:CSS Module 生成的 class 名称属于实现细节,不得依赖。`data-dsh-plugin` + `data-dsh-part` 与文档列出的 `--agsc-*` 变量才是公共稳定接口;新增、重命名或移除 part/变量属于公共契约变更,必须同步测试、本文档与 CHANGELOG。

## daemon 托管策略

- **`adopt-or-host`(默认)**:启动先经 Unix socket `ping` 探测——有活 daemon 即**领养**(只连接、周期健康检查,不掌握生死);没有则(仅 macOS)经 `agent-sidecar service status` **只读**检测 LaunchAgent,已安装则**让位**(DEFER:launchd 负责拉活,插件周期重探、绝不 spawn,避免双托管);两者皆无才自行 spawn `<sidecar.command> daemon run` 作为受监督前台子进程托管。
- **`adopt-only`**:只探测、只领养,绝不 spawn(无 daemon 时停在 DEFER 周期重探,ping 通即领养)。
- **`off`**:不管理 daemon 生命周期——但数据对账照常跑:`off` 的语义是「生死不归我管」,不是「不读数据」。
- **失败处理**:托管失败(spawn 失败、5 秒就绪超时、进程退出)走有界指数退避(1s→2s→4s→…→30s 封顶);连续失败达 `daemon.backoffLimit`(默认 5)次进入 FAILED,停止重启、看板降级提示,绝不无限重启。退避后重探时若外部出现了 daemon,直接领养而非再次 spawn。
- **所有权铁律**:插件只会终止**自己 spawn** 的 daemon(SIGTERM → 5s 宽限 → SIGKILL,进程树范围);对领养的、launchd 管理的外部 daemon,插件卸载/重载时只断连接,**绝不杀**。
- 受托管 daemon 的 stdout/stderr 按行(截断至 400 字符)转发进 dsh 日志(stdout→debug,stderr→warn)。

## 安全与信任姿态

- 插件自开路由 `/plugins/agent-sidecar/api` 不在 dsh `/api` 栅栏覆盖内,故自带五层守卫;**即使 dsh 以 `--host 0.0.0.0` 启动,本插件路由对非回环请求一律 403**。
- 五层守卫:① 对端地址必须为 loopback;② 必须恰有一个合法回环 `Host`(防 DNS rebinding),既有任意合法端口合同不变;③ 当前载体是明文 HTTP,`Origin` 出现时必须恰有一个且其 HTTP host/有效端口与 `Host` 匹配,HTTPS Origin 拒绝,重复 `Host`/`Origin` 即使同值也关闭失败,显式 `sec-fetch-site: cross-site` 拒绝;④ POST/PUT/PATCH 强制 `Content-Type: application/json`(否则 415,阻断跨站简单请求);⑤ 写动作门——`inject.enabled` 默认关,关闭时服务端直接 403。
- 在写门之上还有逐次确认:每次注入必经服务端签发一次性短时效 confirmToken 的两阶段流程(`inject.prepare` → 确认对话框 → `inject.execute`),无批量/定时注入;`delivery: "unknown"` 一律不自动重试、UI 不提供重发按钮。
- 外部 agent 注入总是经 `send --agent <agent> --exact-session --message-stdin` 走精确复合绑定;消息经 stdin,不进 sidecar argv。Kimi 0.38.0 的提示词只进入 ACP NDJSON,不进 Kimi 子进程 argv;cursor-cli 的原生子进程 argv 暴露为其上游恢复契约,确认框如实警示(见主仓 [SECURITY.md](../SECURITY.md))。回执身份不匹配按 delivery unknown 关闭失败。
- AI 旁路分析默认关(消耗模型 token);分析会话有界(输入截断、回合超时、并发上限)、可随时停止,无自动/周期分析;分析正文(摘要、追问、模型回复)绝不写入插件日志。
- **诚实边界**:五层守卫防御的是浏览器介导攻击(CSRF、DNS rebinding、跨站请求),**不防**能直接连 loopback 的本机任意进程——这与 dsh 自身 `/api` 的无认证信任水位持平,弱于 sidecar 自身两面(Unix socket 靠同 UID 0600 文件权限,HTTP 读面要求 Bearer token),属显式声明的权衡而非沉默继承。因此多用户主机不建议开启 `inject.enabled`,读面(会话事件数据)对本机进程可见这一事实请知悉;confirmToken 对直连 loopback 的本机进程不设防,「用户同回合明确请求」在此信道上退化为 UX 约定。
- **本地路径与外发证据**:面向本机 owner 的看板、项目视图、详情和 `/sidecar` 摘要可以显示完整项目路径,用于区分本地工作区;这不表示该路径适合公开。对外发送截图、JSON、日志或支持材料前,必须脱敏项目/转录路径、会话/请求 ID 与正文。
- host 经 Unix socket(同 UID、socket 0600)直连 daemon,不读也绝不外泄 sidecar 的 `http.token`;浏览器永不直连 sidecar HTTP。
- sidecar 本体(CLI/daemon)的威胁模型与红线见主仓 [SECURITY.md](../SECURITY.md)。

## 开发

```sh
cd plugin
pnpm install
pnpm typecheck   # host + client 双 TS program(tsc --noEmit)
pnpm build       # tsdown 双构建 → lib/index.js(host)+ lib/client.js(browser)
pnpm test        # vitest
```

### 浏览器验收

最近一次真实 `dsh_web` 浏览器验收已通过以下检查:

- 亮色、暗色与窄屏响应式布局;
- 主侧栏与 footer 打开同一个 `shell.overlay` 宿主 Modal;
- 外层及嵌套对话框的初始焦点、焦点约束与恢复,每个下层 dialog 的
  `inert` + `aria-hidden` 隔离,以及原有宿主属性的精确恢复;
- 文字、禁用态、状态徽标与焦点指示的可访问对比度;
- 无 console error/warning、页面错误、失败请求或非 2xx 响应。

宿主 locale 桥及语言变更订阅有自动化测试覆盖;此次真实浏览器验收未操作可靠的宿主
locale 控件,因此不把浏览器语言切换列为已验证项。

主仓治理:任何入库变更前在仓库根运行 `python3 scripts/check.py`(见主仓 [CONTRIBUTING.md](../CONTRIBUTING.md))。

目录一览:

```
plugin/
├── cordis.patch.yml        # bundle patch:插入组合行(刻意无 config 块,默认值由 schema 兜底)
├── src/                    # host 半区(Node,cordis 插件,exports ".")
│   ├── index.ts            #   总装入口(named exports:name / inject / Config / apply)
│   ├── config.ts           #   schemastery 配置 schema(上文配置表的事实来源)
│   ├── supervisor.ts       #   daemon 生命周期状态机(probe-adopt-else-host)
│   ├── bridge.ts           #   Unix socket 客户端 + 快照对账器
│   ├── session-store.ts    #   会话快照缓存
│   ├── routes.ts           #   /plugins/agent-sidecar/api 路由(state / stream / session / timeline / lineage / search / projects / action)
│   ├── guard.ts            #   五层请求守卫
│   ├── inject-gateway.ts   #   注入网关(两阶段 confirmToken + 双通路分派)
│   ├── dsh-inject.ts / send-cli.ts   # dsh 进程内注入 / 外部 agent send CLI 执行器
│   ├── fusion.ts           #   sessionQuery × sidecar 事件融合(时间线/谱系/检索/项目)
│   ├── analysis.ts         #   AI 旁路分析引擎(专用 dsh 分析会话,有界)
│   └── skills-provider.ts  #   skill 内嵌提供器(registerProvider 路径)
├── src/client/             # browser 半区(React 18,exports "./client",lazy-CJS 注入 dsh Web)
│   ├── index.ts            #   挂载点注册 + apply-guard 幂等 + 样式生命周期
│   ├── controller.ts / api.ts / sse.ts      # 数据控制器与同源 fetch / SSE 传输
│   ├── board/ · widget.tsx · settings-card.tsx · mount.tsx   # 看板 / 小件 / 设置卡
│   ├── detail/ · dsh-tools/ · inject/ · analysis/   # 详情时间线 / 谱系与检索 / 注入面板 / 分析面板
│   ├── commands.ts · sidebar-tab.tsx   # /sidecar 斜杠命令 / better-sidebar 可选 Tab
│   ├── navigation/ · theme/             #   Agent Center 入口 / 稳定 part 与 --agsc-* 契约
│   └── locales/            #   中英双语文案 + 宿主 locale 桥(缺席时回退 zh)
├── test/                   # vitest 单测与集成测试
├── lib/                    # 预构建产物(随 npm 发布,安装免构建)
├── tsconfig.host.json / tsconfig.client.json
└── tsdown.config.ts / tsdown.client.ts
```

## 许可证与主仓关系

MIT License(与主仓一致,见 [LICENSE](../LICENSE))。

本包是 agent_sidecar 单仓的 `plugin/` 子目录独立 npm 包(`package.json` 中 `repository.directory: "plugin"`):插件与 sidecar 的 CLI/daemon 契约同仓演进、版本同步;Python 主体保持零运行时依赖,Node 工具链只存在于本目录内。sidecar 本体的安装、命令与能力边界以主仓 [README](../README.md)([中文](../README.zh.md))为准。
