/**
 * Unit coverage for the skill provider path two (src/skills-provider.ts)
 * and its index.ts wiring.
 *
 * The fake skills service mirrors the registry contract this module
 * consumes (installed @deepseek-ai/dsh-skill 0.1.1-rc.2 d.ts:249):
 * `registerProvider(create)` invokes the factory synchronously with a
 * lifecycle control and returns an unregister disposer; duplicate provider
 * names throw. The dedup-behavior facts the yield rule rests on (filesystem
 * roots rank 100-500 < BUNDLED 600 within a layer; nearest scope layer wins
 * across layers) were live-verified against real dsh web and recorded in
 * `.local/tasks/make_dsh_mode/design/env_facts.md` §1 — the rank pin test
 * below guards the contract side of that record.
 */
import { describe, expect, it } from 'vitest'

import { apply, Config, type HostContext } from '../src/index'
import {
  registerSidecarSkillProvider,
  SIDECAR_SKILL_CANDIDATE,
  SIDECAR_SKILL_CONTENT,
  SIDECAR_SKILL_DESCRIPTION,
  SIDECAR_SKILL_NAME,
  SIDECAR_SKILL_RANK,
  SKILL_PROVIDER_NAME,
  type SkillProviderControlFace,
  type SkillProviderFace,
  type SkillsServiceFace,
} from '../src/skills-provider'

// ---------------------------------------------------------------------------
// Fakes.
// ---------------------------------------------------------------------------

interface FakeSkillsService extends SkillsServiceFace {
  /** Providers currently registered, by name. */
  registered: Map<string, SkillProviderFace>
  /** Names ever passed to registerProvider, in order. */
  registrationOrder: string[]
  /** Lifecycle controls handed to each factory, by provider name. */
  controls: Map<string, SkillProviderControlFace>
}

function createFakeSkills(): FakeSkillsService {
  const registered = new Map<string, SkillProviderFace>()
  const registrationOrder: string[] = []
  const controls = new Map<string, SkillProviderControlFace>()
  return {
    registered,
    registrationOrder,
    controls,
    registerProvider(create) {
      const lifecycle = new AbortController()
      const control: SkillProviderControlFace = {
        signal: lifecycle.signal,
        invalidate: () => {},
      }
      const provider = create(control)
      if (registered.has(provider.name)) {
        throw new Error(
          `a skill provider named "${provider.name}" is already registered`,
        )
      }
      registered.set(provider.name, provider)
      registrationOrder.push(provider.name)
      controls.set(provider.name, control)
      return () => {
        registered.delete(provider.name)
        lifecycle.abort(new Error(`skill provider "${provider.name}" disposed`))
      }
    },
  }
}

type LogEntry = { level: string; msg: string; meta?: Record<string, unknown> }

function createLog(): { entries: LogEntry[]; log: (level: 'debug' | 'info' | 'warn' | 'error', msg: string, meta?: Record<string, unknown>) => void } {
  const entries: LogEntry[] = []
  return {
    entries,
    log: (level, msg, meta) => {
      entries.push(meta === undefined ? { level, msg } : { level, msg, meta })
    },
  }
}

// ---------------------------------------------------------------------------
// Registration shape.
// ---------------------------------------------------------------------------

describe('registerSidecarSkillProvider', () => {
  it('registers one provider whose catalog is exactly the agent-sidecar candidate', async () => {
    const skills = createFakeSkills()
    const { log } = createLog()

    const dispose = registerSidecarSkillProvider({ skills, provide: true, log })

    expect(dispose).toBeTypeOf('function')
    expect(skills.registrationOrder).toEqual([SKILL_PROVIDER_NAME])
    const provider = skills.registered.get(SKILL_PROVIDER_NAME)!
    expect(provider.name).toBe(SKILL_PROVIDER_NAME)

    const candidates = await provider.list({})
    expect(candidates).toHaveLength(1)
    const candidate = candidates[0]!
    expect(candidate).toMatchObject({
      name: SIDECAR_SKILL_NAME,
      description: SIDECAR_SKILL_DESCRIPTION,
      invocation: { modelInvocable: true, userInvocable: true },
      source: 'bundled',
      provider: SKILL_PROVIDER_NAME,
      rank: SIDECAR_SKILL_RANK,
    })
    // Registry validateCandidate contract: kebab-case name, non-empty
    // description, candidate.provider === provider.name, finite rank.
    expect(/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(candidate.name)).toBe(true)
    expect(candidate.description.length).toBeGreaterThan(0)
    expect(candidate.provider).toBe(provider.name)
    expect(Number.isFinite(candidate.rank)).toBe(true)
    expect(candidate.resourceBase?.kind).toBe('opaque')
  })

  it('serves the full definition for its own candidate and undefined otherwise', async () => {
    const skills = createFakeSkills()
    const { log } = createLog()
    registerSidecarSkillProvider({ skills, provide: true, log })
    const provider = skills.registered.get(SKILL_PROVIDER_NAME)!

    const definition = await provider.get(SIDECAR_SKILL_CANDIDATE, {})
    expect(definition).toMatchObject({
      name: SIDECAR_SKILL_NAME,
      description: SIDECAR_SKILL_DESCRIPTION,
      provider: SKILL_PROVIDER_NAME,
      source: 'bundled',
      content: SIDECAR_SKILL_CONTENT,
    })
    // Semantic anchors of the dsh-scene body (condensed from the canonical
    // skills/agent-sidecar/SKILL.md): observation via CLI + board, dsh
    // injection routed to the plugin panel (unsupported_dsh), and the
    // S5/S6 wording preserved.
    const content = definition!.content
    expect(content).toContain('agent-sidecar status --json')
    expect(content).toContain('Sidecar')
    expect(content).toContain('unsupported_dsh')
    expect(content).toContain('explicitly requests')
    expect(content).toContain('--allow-write')
    expect(content).toContain('delivery: "unknown"')
    expect(content).not.toContain('audit reset --allow-write --confirm')

    const stranger = await provider.get(
      { ...SIDECAR_SKILL_CANDIDATE, name: 'someone-else' },
      {},
    )
    expect(stranger).toBeUndefined()
  })

  it('pins the yield marker: BUNDLED rank 600, above every filesystem root rank', () => {
    // The complete yield rule (env_facts.md §1): dsh dedupes natively; in
    // single-layer topologies rank decides and filesystem roots are
    // project-dsh=100 / project-agents=200 / custom=300 / user-dsh=400 /
    // user-agents=500 — all below this provider's 600, so any
    // user-installed copy shadows the plugin copy. A lower value here
    // would silently steal the catalog entry from user-managed skills.
    expect(SIDECAR_SKILL_RANK).toBe(600)
    for (const filesystemRank of [100, 200, 300, 400, 500]) {
      expect(SIDECAR_SKILL_RANK).toBeGreaterThan(filesystemRank)
    }
    expect(SIDECAR_SKILL_CANDIDATE.rank).toBe(SIDECAR_SKILL_RANK)
  })

  // -------------------------------------------------------------- gate

  it('does not register when skill.provide is off', () => {
    const skills = createFakeSkills()
    const { entries, log } = createLog()

    const dispose = registerSidecarSkillProvider({ skills, provide: false, log })

    expect(dispose).toBeNull()
    expect(skills.registered.size).toBe(0)
    expect(entries.some((e) => e.msg.includes('skill.provide=false'))).toBe(true)
  })

  // -------------------------------------------------------- unregister

  it('returns the registry disposer; disposing unregisters and aborts the control', () => {
    const skills = createFakeSkills()
    const { log } = createLog()

    const dispose = registerSidecarSkillProvider({ skills, provide: true, log })!
    expect(skills.registered.has(SKILL_PROVIDER_NAME)).toBe(true)
    const control = skills.controls.get(SKILL_PROVIDER_NAME)!
    expect(control.signal.aborted).toBe(false)

    dispose()
    expect(skills.registered.has(SKILL_PROVIDER_NAME)).toBe(false)
    expect(control.signal.aborted).toBe(true)
  })

  it('answers a duplicate-registration throw with a warn and null, not a crash', () => {
    const skills = createFakeSkills()
    const { entries, log } = createLog()

    const first = registerSidecarSkillProvider({ skills, provide: true, log })
    const second = registerSidecarSkillProvider({ skills, provide: true, log })

    expect(first).toBeTypeOf('function')
    expect(second).toBeNull()
    expect(skills.registered.size).toBe(1)
    expect(
      entries.some(
        (e) => e.level === 'warn' && e.msg.includes('registration failed'),
      ),
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// index.ts wiring (lazy inject on `skills`).
// ---------------------------------------------------------------------------

/**
 * Minimal fake ctx for the entry, mirroring index.test.ts: `inject` runs
 * the callback immediately when every dep is present and never otherwise;
 * everything else is the quiet minimum the assembly needs.
 */
function createEntryCtx(services: Record<string, unknown>): {
  ctx: HostContext
  disposeAll: () => Promise<void>
} {
  const disposers: Array<() => unknown> = []
  const noop = (): void => {}
  const logger = Object.assign(() => logger, {
    info: noop,
    warn: noop,
    error: noop,
    debug: noop,
  })
  const ctx = {
    logger,
    effect(execute: () => () => unknown) {
      const disposer = execute()
      disposers.push(disposer)
      return disposer
    },
    inject(deps: string[], callback: (ctx: unknown) => void) {
      if (!deps.every((dep) => dep in services)) return undefined
      const injected = Object.assign(Object.create(ctx as object), services)
      callback(injected)
      return undefined
    },
    on: () => noop,
    get: (name: string) => services[name],
    webServer: { register: () => noop },
    subprocess: {
      spawn: () => ({
        pid: 1,
        stdin: undefined,
        stdout: undefined,
        stderr: undefined,
        collected: {},
        done: new Promise<never>(() => {}),
        terminate: noop,
        waitForExit: async () => true,
      }),
    },
  } as unknown as HostContext
  return {
    ctx,
    disposeAll: async () => {
      for (const disposer of [...disposers].reverse()) await disposer()
      disposers.length = 0
    },
  }
}

describe('index wiring: skills provider', () => {
  it('registers on apply when the skills service is present (default provide=true)', async () => {
    const skills = createFakeSkills()
    const { ctx, disposeAll } = createEntryCtx({ skills })

    apply(ctx, Config({}))
    expect(skills.registrationOrder).toEqual([SKILL_PROVIDER_NAME])
    expect(skills.registered.has(SKILL_PROVIDER_NAME)).toBe(true)

    await disposeAll()
  })

  it('skips silently when the composition has no skills service', async () => {
    const { ctx, disposeAll } = createEntryCtx({})
    // Nothing to observe but the absence of a throw: the lazy inject on
    // `skills` never fires, so the assembly must come up regardless.
    expect(() => {
      apply(ctx, Config({}))
    }).not.toThrow()
    await disposeAll()
  })

  it('does not register when skill.provide=false', async () => {
    const skills = createFakeSkills()
    const { ctx, disposeAll } = createEntryCtx({ skills })

    apply(ctx, Config({ skill: { provide: false } }))
    expect(skills.registered.size).toBe(0)

    await disposeAll()
  })
})

describe('config: skill.provide', () => {
  it('defaults to true (design §6)', () => {
    const config = Config({})
    expect(config.skill.provide).toBe(true)
  })
})
