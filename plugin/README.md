# @shendeguize/dsh-agent-sidecar

[Agent Sidecar](../README.md) 的原生 dsh 插件:在 dsh Web 里跨 agent 监控本机 AI agent 会话——看板、会话详情时间线、消息注入(默认关)、AI 旁路分析(默认关)、skill 内嵌提供,以及对 sidecar daemon 的自动托管。

## 能力(当前已交付,M1-M3 + skill/sidebar)

- **跨 agent 会话看板**:会话页新增「Sidecar」Tab,展示本机受支持 agent(cursor IDE/CLI、claude、codex、copilot、dsh、kimi)的会话卡片与状态徽标(`working` / `waiting` / `idle` / `dead`;状态为从持久化数据推断的观察值,可能滞后)。
- **会话详情视图**:统一时间线(融合 sidecar 规范化事件与 dsh 进程内实时事件,分页回溯历史,事件缺口如实标注);dsh 会话专属谱系树与全文检索(经 `sessionQuery`,该服务缺席时优雅降级——检索退化为标题/项目过滤,谱系显示不可用提示);项目分组视图(同一项目下跨 agent 会话并排呈现)。
- **消息注入(默认关)**:`inject.enabled` 开启后,对 waiting/idle 目标经两阶段确认(服务端签发一次性 confirmToken)注入消息——dsh 会话走进程内 queue/steer(`ctx.agents` followup/steer),外部 agent(claude/codex/cursor-cli)经 `agent-sidecar send --message-stdin --allow-write --json` 子进程执行(消息经 stdin 传输,绝不进 sidecar argv)。`delivery: unknown` 回执不提供重试按钮;copilot/kimi/cursor-ide 无注入通道,入口置灰。
- **AI 旁路分析(默认关)**:`analysis.enabled` 开启后,可对被观测会话/项目拉起专用 dsh 分析会话(有界摘要注入 + 增量追问 + 随时停止);模型路由见配置表 `analysis.provider` / `analysis.model`。分析正文绝不写入插件日志。
- **footer 状态小件**:侧边栏底部常驻连接状态点(sidecar 连接态 + 速览)。
- **`/sidecar` 斜杠命令**:会话输入框内的只读状态速览(daemon 状态、连接健康度、working/waiting 计数、按项目分组的活跃会话)。
- **设置卡**:dsh 设置页出现「Agent Sidecar」卡片(设置命名空间 `agent-sidecar`)。
- **daemon 托管**:探测-领养-否则托管(probe-adopt-else-host)策略管理 sidecar daemon 生命周期,详见[下文](#daemon-托管策略)。
- **实时数据面**:host 半区经 Unix socket 消费 daemon(`status` 快照对账 + `subscribe` 事件触发),浏览器经同源 SSE 实时刷新。
- **skill 内嵌提供**:`skill.provide`(默认开)经 dsh skill 注册表提供 agent-sidecar skill,装插件即得、无需运行安装脚本;文件系统已安装的同名 skill 自动优先(dsh 注册表按名合并,文件系统层胜出,无需探测)。
- **better-sidebar 可选 Tab**(软依赖):装有 `dsh-better-sidebar` 时注册紧凑「Sidecar」侧边 Tab(连接点 + 计数 + 最近会话);未装静默跳过;Tab 不可见时释放订阅,不额外轮询、不另建 SSE。
- **中英双语界面**(默认中文)。

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

装好后在三处出现:

1. 会话页顶部 Tab 环 →「Sidecar」看板;
2. 侧边栏 footer → 状态小件;
3. 设置页插件区 →「Agent Sidecar」设置卡。

命令行验证:`curl http://127.0.0.1:<port>/plugins/agent-sidecar/api/state` 应返回含 `daemon` / `board` / `capabilities` 的 JSON 快照;`GET …/api/stream` 为 SSE 流;`GET …/api/session/<id>` 为单会话详情(含融合时间线首页),`GET …/api/session/<id>/timeline?cursor=&limit=` 分页回溯历史;`GET …/api/lineage/<id>`、`GET …/api/search?q=`、`GET …/api/projects` 为 M3 读面;`POST …/api/action` 为幂等动作信封(`inject.prepare` / `inject.execute` / `analysis.*` / `daemon.retry`)。

## 配置

配置走双轨:

- **组合配置**(profile 的 `cordis.patch.yml` 中对 `id: agent-sidecar` 行加 `config:` 块):schemastery 校验,所有字段带默认值,零配置即可挂载。层叠对 `config` 是**整块替换**而非深度合并,覆写时请写全所需字段组。
- **设置卡**(dsh 设置页,命名空间 `agent-sidecar`):同一张表,可视化编辑并持久化到 dsh 设置文档。

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
| `analysis.provider` | `string` | `''` | 分析代理的 provider 路由:留空(默认)复用宿主默认模型(`agentDefaultModel` 服务,与 dsh 自身入口同源);与 `analysis.model` 同时非空才生效 |
| `analysis.model` | `string` | `''` | 分析代理的模型 id:留空(默认)复用宿主默认模型;与 `analysis.provider` 同时非空才生效。两者留空且宿主无默认模型时,`analysis.request` 被拒为 `analysis_model_unconfigured` |
| `ui.timeWindowHours` | 自然数(≥1) | `24` | 看板会话时间窗(小时) |
| `ui.showDead` | `boolean` | `false` | 是否显示 dead 会话 |
| `skill.provide` | `boolean` | **`true`(默认开)** | 是否经 registerProvider 内嵌提供 agent-sidecar skill;文件系统已安装的同名 skill 自动优先;装配时读取,改动需重载插件生效 |

### 生效方式(如实说明)

- `inject.enabled`:**即时生效**(host 侧实时读取,守卫立即响应)。
- `analysis.*`:**即时生效**(开关与 provider/model 均按次实时读取)。
- `ui.*`:**即时生效**(浏览器侧实时读取,作为看板筛选默认值;用户手动筛选后以用户选择为准)。
- `skill.provide`:**装配时读取**(重启语义)——改动需重载插件生效。
- `daemon.*` / `stream.*` / `sidecar.*`:当前**以组合配置(patch 的 `config:` 块)为准**,在插件装配时烘焙定型;设置卡内对这三组的修改暂不驱动运行时(完整的重启重读接线属后续里程碑)。要改这三组,请改 profile patch 后重启 dsh。

## daemon 托管策略

- **`adopt-or-host`(默认)**:启动先经 Unix socket `ping` 探测——有活 daemon 即**领养**(只连接、周期健康检查,不掌握生死);没有则(仅 macOS)经 `agent-sidecar service status` **只读**检测 LaunchAgent,已安装则**让位**(DEFER:launchd 负责拉活,插件周期重探、绝不 spawn,避免双托管);两者皆无才自行 spawn `<sidecar.command> daemon run` 作为受监督前台子进程托管。
- **`adopt-only`**:只探测、只领养,绝不 spawn(无 daemon 时停在 DEFER 周期重探,ping 通即领养)。
- **`off`**:不管理 daemon 生命周期——但数据对账照常跑:`off` 的语义是「生死不归我管」,不是「不读数据」。
- **失败处理**:托管失败(spawn 失败、5 秒就绪超时、进程退出)走有界指数退避(1s→2s→4s→…→30s 封顶);连续失败达 `daemon.backoffLimit`(默认 5)次进入 FAILED,停止重启、看板降级提示,绝不无限重启。退避后重探时若外部出现了 daemon,直接领养而非再次 spawn。
- **所有权铁律**:插件只会终止**自己 spawn** 的 daemon(SIGTERM → 5s 宽限 → SIGKILL,进程树范围);对领养的、launchd 管理的外部 daemon,插件卸载/重载时只断连接,**绝不杀**。
- 受托管 daemon 的 stdout/stderr 按行(截断至 400 字符)转发进 dsh 日志(stdout→debug,stderr→warn)。

## 安全与信任姿态

- 插件自开路由 `/plugins/agent-sidecar/api` 不在 dsh `/api` 栅栏覆盖内,故自带五层守卫;**即使 dsh 以 `--host 0.0.0.0` 启动,本插件路由对非回环请求一律 403**。
- 五层守卫:① 对端地址必须为 loopback;② `Host` 必须为回环权威(防 DNS rebinding);③ `Origin`(出现时)必须与 Host 同源,显式 `sec-fetch-site: cross-site` 拒绝;④ POST/PUT/PATCH 强制 `Content-Type: application/json`(否则 415,阻断跨站简单请求);⑤ 写动作门——`inject.enabled` 默认关,关闭时服务端直接 403。
- 在写门之上还有逐次确认:每次注入必经服务端签发一次性短时效 confirmToken 的两阶段流程(`inject.prepare` → 确认对话框 → `inject.execute`),无批量/定时注入;`delivery: "unknown"` 一律不自动重试、UI 不提供重发按钮。
- 外部 agent 注入的消息经 `send --message-stdin` 走 stdin,不进 sidecar argv;cursor-cli 的原生子进程 argv 暴露为其上游恢复契约,确认框如实警示(见主仓 [SECURITY.md](../SECURITY.md))。
- AI 旁路分析默认关(消耗模型 token);分析会话有界(输入截断、回合超时、并发上限)、可随时停止,无自动/周期分析;分析正文(摘要、追问、模型回复)绝不写入插件日志。
- **诚实边界**:五层守卫防御的是浏览器介导攻击(CSRF、DNS rebinding、跨站请求),**不防**能直接连 loopback 的本机任意进程——这与 dsh 自身 `/api` 的无认证信任水位持平,弱于 sidecar 自身两面(Unix socket 靠同 UID 0600 文件权限,HTTP 读面要求 Bearer token),属显式声明的权衡而非沉默继承。因此多用户主机不建议开启 `inject.enabled`,读面(会话事件数据)对本机进程可见这一事实请知悉;confirmToken 对直连 loopback 的本机进程不设防,「用户同回合明确请求」在此信道上退化为 UX 约定。
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
│   └── locales/            #   中英双语文案(默认 zh)
├── test/                   # vitest 单测与集成测试
├── lib/                    # 预构建产物(随 npm 发布,安装免构建)
├── tsconfig.host.json / tsconfig.client.json
└── tsdown.config.ts / tsdown.client.ts
```

## 许可证与主仓关系

MIT License(与主仓一致,见 [LICENSE](../LICENSE))。

本包是 agent_sidecar 单仓的 `plugin/` 子目录独立 npm 包(`package.json` 中 `repository.directory: "plugin"`):插件与 sidecar 的 CLI/daemon 契约同仓演进、版本同步;Python 主体保持零运行时依赖,Node 工具链只存在于本目录内。sidecar 本体的安装、命令与能力边界以主仓 [README](../README.md)([中文](../README.zh.md))为准。
