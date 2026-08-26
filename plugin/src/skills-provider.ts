/**
 * Skill path two (design §7): register the `agent-sidecar` skill on dsh's
 * `ctx.skills` registry via `registerProvider`, so installing the plugin
 * yields the skill without running `scripts/install-skill.sh`.
 *
 * Coexistence with the filesystem path (design risk 8, resolved 2026-08-25
 * by live test + source, recorded in
 * `.local/tasks/make_dsh_mode/design/env_facts.md`): dsh's SkillRegistry
 * natively resolves same-name skills to a SINGLE catalog entry — the merged
 * catalog is a by-name Map, so a double listing is structurally impossible
 * — and the filesystem copy always wins when present, via two mechanisms:
 * within one layer, candidates sort by rank (lower wins) and same-name
 * losers are dropped with a warn log (source `collectLayer`; filesystem
 * roots rank 100-500 vs BUNDLED_SKILL_RANK 600); across layers, the
 * nearest scope layer replaces farther ones outright — which is the
 * operative rule in dsh-web compositions, where skill-filesystem is
 * host-disabled and mounted per agent-preset scope while this provider
 * sits in the global layer (live-verified: with `~/.dsh/skills/
 * agent-sidecar` present the RPC catalog showed exactly one entry, the
 * filesystem one; removing it flipped the same entry to this provider's
 * without a restart). The yield rule therefore needs no filesystem
 * probing: registering at BUNDLED_SKILL_RANK (600, the sanctioned rank for
 * packaged providers, installed d.ts:16) is the complete, TOCTOU-free
 * yield marker in every topology.
 *
 * The skill body is a dsh-scene condensation of the canonical
 * `skills/agent-sidecar/SKILL.md` (same repo): observation flows through
 * the CLI and this plugin's web board, while injection uses either the
 * plugin's in-process DSH path or the sidecar send boundary for supported
 * external agents. Direct sidecar `send` remains `unsupported_dsh`; that
 * does not make the plugin's DSH injection unsupported. The S5/S6 wording
 * — send only on an explicit same-turn request, never retry
 * `delivery: "unknown"` — is preserved verbatim in spirit. The body is
 * embedded (not read from disk) so the npm package needs no extra assets;
 * `resourceBase` is `opaque` pointing readers at the canonical repo docs.
 *
 * Faces below are structural on purpose (repo rule: the plugin's type
 * surface stays on the devDependency SDKs; service packages resolve at
 * runtime from the dsh profile tree).
 *
 * @module
 */

/** Invocation controls mirrored from @deepseek-ai/dsh-skill (d.ts:37-42). */
export interface SkillInvocationFace {
  readonly modelInvocable: boolean
  readonly userInvocable: boolean
}

/** Provider-declared resource base (opaque flavor only; d.ts:32-35). */
export interface SkillResourceBaseFace {
  readonly kind: 'opaque'
  readonly description: string
}

/** Catalog candidate returned by `provider.list()` (d.ts:61-70). */
export interface SkillCandidateFace {
  readonly name: string
  readonly description: string
  readonly whenToUse?: string
  readonly invocation: SkillInvocationFace
  readonly source: string
  readonly provider: string
  readonly resourceBase?: SkillResourceBaseFace
  /** Lower ranks win same-name duplicates within one layer (d.ts:62-63). */
  readonly rank: number
  /** Opaque provider-owned handle passed back to `provider.get()`. */
  readonly locator: unknown
}

/** Full definition returned by `provider.get()` (d.ts:71-79). */
export interface SkillDefinitionFace {
  readonly name: string
  readonly description: string
  readonly invocation: SkillInvocationFace
  readonly source: string
  readonly provider: string
  readonly resourceBase?: SkillResourceBaseFace
  readonly content: string
}

/** Lookup options are borrowed opaquely; this provider is context-free. */
export interface SkillLookupOptionsFace {
  readonly cwd?: string | undefined
  readonly signal?: AbortSignal | undefined
}

/** One same-process skill source (d.ts:168-188). */
export interface SkillProviderFace {
  readonly name: string
  readonly list: (
    options: SkillLookupOptionsFace,
  ) => Promise<readonly SkillCandidateFace[]>
  readonly get: (
    candidate: SkillCandidateFace,
    options: SkillLookupOptionsFace,
  ) => Promise<SkillDefinitionFace | undefined>
}

/** Registration-scoped lifecycle control handed to the factory (d.ts:189-195). */
export interface SkillProviderControlFace {
  readonly signal: AbortSignal
  readonly invalidate: () => void
}

/**
 * `ctx.skills` face (provider registration only). Source: installed
 * @deepseek-ai/dsh-skill 0.1.1-rc.2 lib/types/index.d.ts:249 —
 * `registerProvider(create): () => void`. Registration is synchronous
 * during plugin apply, rides the CALLER's fiber (duplicate provider names
 * in one layer throw), and the returned disposer is the exact cordis
 * effect disposer that unregisters the provider.
 */
export interface SkillsServiceFace {
  registerProvider(
    create: (control: SkillProviderControlFace) => SkillProviderFace,
  ): () => void
}

/** Log face shared with the rest of the host half. */
export type SkillsProviderLog = (
  level: 'debug' | 'info' | 'warn' | 'error',
  msg: string,
  meta?: Record<string, unknown>,
) => void

/** Registry name of this provider (distinct from the skill it serves). */
export const SKILL_PROVIDER_NAME = 'agent-sidecar-plugin'

/** The one skill this provider serves (same name as the filesystem copy). */
export const SIDECAR_SKILL_NAME = 'agent-sidecar'

/**
 * BUNDLED_SKILL_RANK, pinned from @deepseek-ai/dsh-skill 0.1.1-rc.2
 * (lib/types/index.d.ts:16, "Standard precedence rank for packaged skill
 * providers and local bundled roots"). This IS the yield rule: filesystem
 * roots rank 100/200/300/400/500, all lower, so any user-managed copy of
 * the skill shadows this packaged one (live-verified 2026-08-25).
 */
export const SIDECAR_SKILL_RANK = 600

/** Routing description; aligned with skills/agent-sidecar/SKILL.md frontmatter. */
export const SIDECAR_SKILL_DESCRIPTION =
  'Monitors readonly local AI agent sessions (claude/codex/cursor/dsh/kimi/copilot) and reports ' +
  'their state and progress via the agent-sidecar CLI and the Sidecar board in dsh web. Use when ' +
  'the user asks for agent status, session progress, to monitor agents, which agent is waiting or ' +
  'working, or explicitly asks to send a message or feedback to an agent.'

/**
 * dsh-scene skill body: semantically consistent with the canonical
 * `skills/agent-sidecar/SKILL.md`, condensed for the plugin context —
 * observation goes CLI/board, while mutation distinguishes protected Kimi
 * spawn-resume, in-process DSH injection, and the external send CLI path.
 */
export const SIDECAR_SKILL_CONTENT = `# Agent Sidecar (dsh plugin edition)

This dsh composition runs the \`dsh-agent-sidecar\` plugin. Observation is
the default; every mutation needs an explicit user request in the same turn.

## Observe

1. Check \`command -v agent-sidecar\`. If missing, do not install anything
   unless the user explicitly asks; point them at the agent_sidecar repo
   install options instead.
2. Run \`agent-sidecar status --json\` first; summarize sessions by agent,
   status, title, project, and age from \`updated_at\`.
3. Other observation commands, only when they match the request:
   \`list --json\` (48h window), \`list --all --json\`, \`ps --json\`,
   \`watch <session-prefix> --json\`, \`watch --all --json\`, \`tui\`.
4. The plugin also serves a live multi-agent board in dsh web (the
   "Sidecar" conversation tab). Prefer pointing the user there for
   continuous monitoring instead of polling the CLI yourself.
5. Treat \`working\`/\`waiting\` as inferred observations from persisted
   data, not control-plane guarantees; Cursor IDE can report \`waiting\`
   several minutes late.

## Inject (explicit request only)

- For **Kimi Code 0.38.0**, the only supported mutation is protected ACP
  spawn-resume for a local, top-level \`waiting\` or \`idle\` session.
  \`working\`, \`dead\`, child/sidechain, and remote Kimi sessions are
  rejected. The plugin UI fixes the internal request mode to \`queue\`, but
  presents this operation as **Protected resume**, not queueing or steering:
  it starts a separate Kimi ACP process, resumes persisted state, and never
  attaches to or steers an existing terminal.
- Kimi receives the message in the ACP JSON-RPC NDJSON stream, never in the
  Kimi process argv. The resumed ACP session is put in default/manual mode;
  every permission request or question is answered \`cancelled\`, never
  approved. Even when Kimi returns \`outcome: "completed"\`, durable delivery
  cannot be proven: the receipt remains \`delivery: "unknown"\`. Do not
  automatically or manually retry the same content. Replaying the same retained
  \`request_id\` is safe: it returns the cached result without spawning
  another ACP process. An older Sidecar may return \`unsupported_kimi\`;
  report that as a compatibility limit, not as a claim that current Kimi
  support is absent.
- For **dsh sessions**, use the plugin panel. A loaded live Agent supports
  \`queue\` via \`followup\` and \`steer\` via \`steer\`, reusing that
  Agent's existing model route and preset. A non-live \`waiting\`/\`idle\`
  session may use guarded cold resume. Cold resume requires a complete
  current default provider/model pair (\`dsh_model_unconfigured\` otherwise)
  and rejects any proven explicit or implicit preset
  (\`dsh_preset_unsupported\`); unknown persistence, preset, or host-service
  state fails closed. Direct \`agent-sidecar send\` still returns
  \`unsupported_dsh\`; only that CLI path is unsupported, not DSH injection
  through this plugin.
- For **claude / codex / cursor-cli** sessions in \`waiting\`/\`idle\`, use
  the plugin panel, or run \`send\` only when the user explicitly requests
  the exact message or action in the same turn. Never infer consent from a
  request to observe, watch, report, or wait. That explicit same-turn
  request is the permission required to use \`--allow-write\`; never add it
  otherwise:

  \`\`\`sh
  agent-sidecar send <session-prefix> "<exact-message>" --allow-write --request-id "<stable-unique-id>" --json
  \`\`\`

- On the external \`agent-sidecar send\` path, preserve the returned
  \`request_id\` and \`replayed\` fields. It rejects remote, \`working\`,
  \`dead\`, child, and unsupported-agent sessions. \`cursor-ide\` and
  \`copilot\` have no mutation path; the plugin's in-process DSH rules above
  are separate.
- Never retry \`failed\`, \`timed_out\`, \`request_pending\`,
  \`audit_error\`, \`cleanup_incomplete\`, or any result with
  \`delivery: "unknown"\` — the agent may already have received the
  message. Report the unknown state plainly and ask the user what to do.
- The audit store is fail-closed; never run \`agent-sidecar audit reset\`
  automatically.

## Reference

Full schemas, exit codes, and boundaries: \`skills/agent-sidecar/SKILL.md\`
and \`reference.md\` in the agent_sidecar repository (also installable as a
filesystem skill via \`scripts/install-skill.sh\`; a user-managed filesystem
copy automatically shadows this plugin-provided one).`

const RESOURCE_BASE: SkillResourceBaseFace = {
  kind: 'opaque',
  description:
    'Self-contained skill provided by the dsh-agent-sidecar plugin; the canonical long-form ' +
    'reference (SKILL.md + reference.md) lives in the agent_sidecar repository under skills/agent-sidecar/.',
}

const INVOCATION: SkillInvocationFace = { modelInvocable: true, userInvocable: true }

/** The single catalog candidate this provider lists (skill-badge template). */
export const SIDECAR_SKILL_CANDIDATE: SkillCandidateFace = {
  name: SIDECAR_SKILL_NAME,
  description: SIDECAR_SKILL_DESCRIPTION,
  invocation: INVOCATION,
  source: 'bundled',
  provider: SKILL_PROVIDER_NAME,
  resourceBase: RESOURCE_BASE,
  rank: SIDECAR_SKILL_RANK,
  locator: SIDECAR_SKILL_NAME,
}

/** Dependencies of {@link registerSidecarSkillProvider}. */
export interface SkillsProviderDeps {
  /** The bound `ctx.skills` registry. */
  skills: SkillsServiceFace
  /** Config gate `skill.provide` (read once at apply; restart semantics). */
  provide: boolean
  log: SkillsProviderLog
}

/** The provider instance: one static candidate, embedded body. */
const provider: SkillProviderFace = {
  name: SKILL_PROVIDER_NAME,
  list: () => Promise.resolve([SIDECAR_SKILL_CANDIDATE]),
  get: (candidate) =>
    Promise.resolve(
      candidate.name === SIDECAR_SKILL_NAME
        ? {
            name: SIDECAR_SKILL_NAME,
            description: SIDECAR_SKILL_DESCRIPTION,
            invocation: INVOCATION,
            source: 'bundled',
            provider: SKILL_PROVIDER_NAME,
            resourceBase: RESOURCE_BASE,
            content: SIDECAR_SKILL_CONTENT,
          }
        : undefined,
    ),
}

/**
 * Register the agent-sidecar skill provider on `ctx.skills`.
 *
 * Yield rule (per live test, env_facts.md): none needed beyond the rank —
 * dsh's registry dedupes same-name skills natively, and this provider's
 * BUNDLED rank (600) loses to every filesystem root, so a filesystem copy
 * always shadows the plugin copy and the catalog shows exactly one entry
 * either way. `provide=false` skips registration entirely.
 *
 * @param deps - registry face, config gate, and log sink.
 * @returns the registry's unregister disposer, or `null` when the gate is
 *   off or registration failed (duplicate provider name in this layer —
 *   only reachable if the plugin is mounted twice in one scope).
 */
export function registerSidecarSkillProvider(
  deps: SkillsProviderDeps,
): (() => void) | null {
  if (!deps.provide) {
    deps.log('debug', 'skill provider disabled (skill.provide=false)')
    return null
  }
  try {
    const dispose = deps.skills.registerProvider(() => provider)
    deps.log('debug', 'skill provider registered', {
      provider: SKILL_PROVIDER_NAME,
      skill: SIDECAR_SKILL_NAME,
      rank: SIDECAR_SKILL_RANK,
    })
    return dispose
  } catch (err) {
    deps.log('warn', `skill provider registration failed: ${String(err)}`)
    return null
  }
}
