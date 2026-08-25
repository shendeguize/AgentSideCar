import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import {
  acquireWithHandoff,
  isRegistrationCollision,
  type HandoffOptions,
  type HandoffScheduler,
} from '../src/client/lifecycle/handoff.ts'

class ManualScheduler implements HandoffScheduler {
  private sequence = 0
  private tasks = new Map<number, { callback: () => void; delayMs: number }>()
  elapsedMs = 0

  setTimeout(callback: () => void, delayMs: number): number {
    const id = ++this.sequence
    this.tasks.set(id, { callback, delayMs })
    return id
  }

  clearTimeout(handle: unknown): void {
    this.tasks.delete(handle as number)
  }

  get pending(): number {
    return this.tasks.size
  }

  runNext(): void {
    const first = [...this.tasks.entries()][0]
    if (first === undefined) return
    const [id, task] = first
    this.tasks.delete(id)
    this.elapsedMs += task.delayMs
    task.callback()
  }

  runAll(): void {
    while (this.tasks.size > 0) this.runNext()
  }
}

function options(
  scheduler: ManualScheduler,
  overrides: Partial<HandoffOptions> = {},
): HandoffOptions {
  return {
    scheduler,
    isCollision: isRegistrationCollision,
    onError: vi.fn(),
    onTimeout: vi.fn(),
    ...overrides,
  }
}

describe('acquireWithHandoff', () => {
  it('hands ownership from the old lease to the waiting new lease', () => {
    const scheduler = new ManualScheduler()
    let owner: 'old' | 'new' | null = null
    const oldDispose = vi.fn()
    const newDispose = vi.fn()
    const old = acquireWithHandoff(() => {
      if (owner !== null) return undefined
      owner = 'old'
      return () => {
        oldDispose()
        owner = null
      }
    }, options(scheduler))
    const next = acquireWithHandoff(() => {
      if (owner !== null) return undefined
      owner = 'new'
      return () => {
        newDispose()
        owner = null
      }
    }, options(scheduler))

    expect(owner).toBe('old')
    expect(scheduler.pending).toBe(1)
    old()
    scheduler.runNext()
    expect(owner).toBe('new')

    next()
    next()
    expect(owner).toBeNull()
    expect(oldDispose).toHaveBeenCalledOnce()
    expect(newDispose).toHaveBeenCalledOnce()
  })

  it('cancels a waiting lease without another attempt', () => {
    const scheduler = new ManualScheduler()
    const register = vi.fn(() => undefined)
    const release = acquireWithHandoff(register, options(scheduler))
    expect(scheduler.pending).toBe(1)

    release()
    scheduler.runAll()
    expect(scheduler.pending).toBe(0)
    expect(register).toHaveBeenCalledOnce()
  })

  it('reports a non-collision once and never retries', () => {
    const scheduler = new ManualScheduler()
    const error = new TypeError('registry unavailable')
    const onError = vi.fn()
    const register = vi.fn(() => {
      throw error
    })
    acquireWithHandoff(register, options(scheduler, { onError }))

    expect(register).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenCalledWith(error)
    expect(scheduler.pending).toBe(0)
  })

  it('bounds a permanent collision to eight attempts and one second', () => {
    const scheduler = new ManualScheduler()
    const onTimeout = vi.fn()
    const register = vi.fn(() => {
      throw new Error('already registered')
    })
    acquireWithHandoff(register, options(scheduler, { onTimeout }))
    scheduler.runAll()

    expect(register).toHaveBeenCalledTimes(8)
    expect(scheduler.elapsedMs).toBe(1_000)
    expect(onTimeout).toHaveBeenCalledOnce()
    expect(scheduler.pending).toBe(0)
  })
})

describe('slot handoff wiring', () => {
  it('returns bounded leases instead of duplicate no-op disposers', () => {
    const source = readFileSync(
      new URL('../src/client/index.ts', import.meta.url),
      'utf8',
    )
    for (const slot of [
      'shell.overlay',
      'conversation.view',
      'sidebar.footer.action',
      'settings.plugin.item',
    ]) {
      expect(source).toContain(`slots.inject('${slot}', () => acquireWithHandoff(`)
    }
    expect(source).not.toMatch(/if \(hasOwnEntry\([^)]*\)\) return \(\) => \{\}/)
  })
})

describe('style lifecycle source contract', () => {
  it('snapshots authoritative CSS text and scopes ownership to one generation', () => {
    const source = readFileSync(
      new URL('../src/client/index.ts', import.meta.url),
      'utf8',
    )

    expect(source).toContain(
      "const STYLE_MANIFEST = Symbol.for('@shendeguize/dsh-agent-sidecar/style-manifest')",
    )
    expect(source).toContain(
      "const STYLE_GENERATION = Symbol.for('@shendeguize/dsh-agent-sidecar/style-generation')",
    )
    expect(source).toContain('const manifest = snapshotStyleManifest(globals)')
    expect(source).toMatch(
      /const cssText = key === undefined \? undefined : manifest\.get\(key\)\s+if \(cssText === undefined\) \{\s+tag\.remove\(\)\s+continue/,
    )
    expect(source).toContain('tag.textContent = cssText')
    expect(source).toContain('for (const [key, cssText] of manifest)')
    expect(source).toContain('cachedStyleManifest?.generation === generation')
    expect(source).toContain('if (isStyleOwner(el, owner)) el.remove()')
  })
})
