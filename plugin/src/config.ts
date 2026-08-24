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

import z from '@deepseek-ai/schemastery'
import type { SupervisorPolicy } from './supervisor.ts'

/** Daemon lifecycle governance (design §4.a). */
export interface DaemonConfig {
  /** adopt-or-host: probe→adopt→else spawn; adopt-only: never spawn; off: no lifecycle management (read-only reconcile still runs). */
  policy: SupervisorPolicy
  /** Consecutive hosting failures before the supervisor trips FAILED. */
  backoffLimit: number
}

/** How to reach/launch the sidecar itself. */
export interface SidecarInvocationConfig {
  /** argv prefix of the sidecar executable (PATH name, absolute path, or e.g. python3+zipapp as multiple entries). */
  command: string[]
  /** Empty = default `~/.agent_sidecar` (honoring AGENT_SIDECAR_RUNTIME_DIR); non-empty redirects via env for spawned daemons. */
  runtimeDir: string
}

/** Reconciler snapshot cadences (design §4.b / ADR-2). */
export interface StreamConfig {
  /** `status` snapshot cadence while any session is working (ms). */
  reconcileActiveMs: number
  /** `status` snapshot cadence otherwise (ms). */
  reconcileIdleMs: number
}

/** Write-path master switch and defaults (M2 consumes defaultMode). */
export interface InjectConfig {
  /** Master gate: false hides all inject affordances and 403s write actions server-side. */
  enabled: boolean
  /** Default injection mode offered by the inject panel. */
  defaultMode: 'queue' | 'steer'
}

/** AI bypass-analysis switch and model routing (M3). */
export interface AnalysisConfig {
  enabled: boolean
  /**
   * Explicit provider route for the dedicated analysis agents. Empty (the
   * default) reuses the host's default model selection (`agentDefaultModel`
   * service, the same source dsh's own entry points read). Takes effect
   * only together with a non-empty `model`.
   */
  provider: string
  /** Explicit model id for the analysis agents; see {@link provider}. */
  model: string
}

/** Board rendering knobs (client half). */
export interface UiConfig {
  /** Session recency window shown on the board (hours). */
  timeWindowHours: number
  /** Whether dead sessions are listed. */
  showDead: boolean
}

/** Skill provider switch (M4). */
export interface SkillConfig {
  provide: boolean
}

/** Validated composition config (all defaults filled by the schema). */
export interface Config {
  daemon: DaemonConfig
  sidecar: SidecarInvocationConfig
  stream: StreamConfig
  inject: InjectConfig
  analysis: AnalysisConfig
  ui: UiConfig
  skill: SkillConfig
}

export const Config: z<Config> = z.object({
  daemon: z
    .object({
      policy: z
        .union([z.const('adopt-or-host'), z.const('adopt-only'), z.const('off')])
        .default('adopt-or-host')
        .description(
          'daemon 托管策略:adopt-or-host=探测并领养既有 daemon,否则自行拉起;adopt-only=只领养绝不拉起;off=不管理 daemon 生命周期(仍只读对账既有 daemon 的数据)',
        ),
      backoffLimit: z
        .natural()
        .min(1)
        .default(5)
        .description('托管失败熔断阈值:连续失败达到该次数后停止重启并进入 FAILED'),
    })
    .description('daemon 生命周期治理'),
  sidecar: z
    .object({
      command: z
        .array(String)
        .default(['agent-sidecar'])
        .description(
          'sidecar 可执行命令(argv 前缀):PATH 名、绝对路径,或多段命令(如 ["python3", "/path/to/agent-sidecar.pyz"]);插件绝不代装',
        ),
      runtimeDir: z
        .string()
        .default('')
        .description(
          '运行时目录:留空用默认 ~/.agent_sidecar(尊重 AGENT_SIDECAR_RUNTIME_DIR 环境变量);非空时经环境变量传给受托管的 daemon',
        ),
    })
    .description('sidecar 调用方式'),
  stream: z
    .object({
      reconcileActiveMs: z
        .natural()
        .min(100)
        .default(2000)
        .description('对账快照周期(有会话工作中,毫秒)'),
      reconcileIdleMs: z
        .natural()
        .min(100)
        .default(10000)
        .description('对账快照周期(空闲,毫秒)'),
    })
    .description('数据流对账节奏'),
  inject: z
    .object({
      enabled: z
        .boolean()
        .default(false)
        .description(
          '注入总开关:关闭时看板隐藏全部注入入口,写接口在服务端同步拒绝(默认关闭;多用户主机不建议开启)',
        ),
      defaultMode: z
        .union([z.const('queue'), z.const('steer')])
        .default('queue')
        .description('注入面板默认模式:queue=排队下一轮,steer=中途注入'),
    })
    .description('消息注入'),
  analysis: z
    .object({
      enabled: z
        .boolean()
        .default(false)
        .description('AI 旁路分析开关(M3;消耗模型 token,默认关闭)'),
      provider: z
        .string()
        .default('')
        .description(
          '分析代理的 provider 路由:留空(默认)复用宿主默认模型(agentDefaultModel 服务);与 model 同时非空才生效',
        ),
      model: z
        .string()
        .default('')
        .description(
          '分析代理的模型 id:留空(默认)复用宿主默认模型;与 provider 同时非空才生效',
        ),
    })
    .description('旁路分析'),
  ui: z
    .object({
      timeWindowHours: z
        .natural()
        .min(1)
        .default(24)
        .description('看板会话时间窗(小时)'),
      showDead: z.boolean().default(false).description('是否显示 dead 会话'),
    })
    .description('看板界面'),
  skill: z
    .object({
      provide: z
        .boolean()
        .default(false)
        .description('是否经 registerProvider 内嵌提供 agent-sidecar skill(M4 启用)'),
    })
    .description('skill 模式'),
})
