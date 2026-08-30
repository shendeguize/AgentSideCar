import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import {
  SIDEBAR_ENTRY_SELECTOR,
  bindSidebarEntryCopy,
  bindSidebarEntryOwner,
  hasSidebarEntry,
  isOwnedSidebarEntry,
  mountSidebarEntry,
  openBoundSidebarEntry,
  type SidebarEntryCopyPort,
} from '../src/client/navigation/sidebar-entry.ts'
import { PLUGIN_DOM_ID } from '../src/client/theme/parts.ts'

const ENTRY_DISPATCHER = Symbol.for('@shendeguize/dsh-agent-sidecar/sidebar-entry-dispatcher')
const SIDEBAR_SOURCE = readFileSync(
  new URL('../src/client/navigation/sidebar-entry.ts', import.meta.url),
  'utf8',
)
const SIDEBAR_CSS = readFileSync(
  new URL('../src/client/navigation/sidebar-entry.module.css', import.meta.url),
  'utf8',
)

class FakeButton {
  readonly tagName = 'BUTTON'
  title = ''
  className = ''
  isConnected = true
  readonly listeners = new Map<string, Set<() => void>>()
  readonly remove = vi.fn(() => { this.isConnected = false })
  cloneCalls = 0
  replacement: FakeButton | null = null
  onReplace: ((replacement: FakeButton) => void) | null = null

  constructor(
    readonly attributes: Map<string, string>,
    readonly label: { textContent: string | null },
  ) {}

  get lastElementChild(): { textContent: string | null } {
    return this.label
  }

  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null
  }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value)
  }

  querySelector(selector: string): { textContent: string | null } | null {
    return selector === '[data-agent-sidecar-sidebar-entry-label]' ? this.label : null
  }

  addEventListener(type: string, listener: () => void): void {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  click(): void {
    for (const listener of this.listeners.get('click') ?? []) listener()
  }

  cloneNode(deep: boolean): FakeButton {
    this.cloneCalls += 1
    const clone = new FakeButton(
      new Map(this.attributes),
      { textContent: deep ? this.label.textContent : null },
    )
    clone.title = this.title
    clone.className = this.className
    return clone
  }

  replaceWith(replacement: FakeButton): void {
    this.replacement = replacement
    this.isConnected = false
    replacement.isConnected = true
    this.onReplace?.(replacement)
  }
}

function legacyButton(): FakeButton {
  return new FakeButton(new Map([
    ['data-agent-sidecar-sidebar-entry', ''],
    ['data-dsh-plugin', PLUGIN_DOM_ID],
    ['data-dsh-part', 'sidebar-entry'],
    ['aria-label', 'Legacy label'],
    ['data-preserved', 'yes'],
  ]), { textContent: 'Legacy Center' })
}

function stubSidebarDocument(initial: FakeButton): {
  current: () => FakeButton
  querySelector: ReturnType<typeof vi.fn>
} {
  let current = initial
  const connectReplacement = (replacement: FakeButton): void => {
    current = replacement
    replacement.onReplace = connectReplacement
  }
  initial.onReplace = connectReplacement
  const querySelector = vi.fn((selector: string) => (
    selector === SIDEBAR_ENTRY_SELECTOR ? current : null
  ))
  vi.stubGlobal('document', { querySelector })
  return { current: () => current, querySelector }
}

describe('sidebar entry', () => {
  it('uses a 16px line-network icon and host-independent narrow centering', () => {
    expect(SIDEBAR_SOURCE).toContain("viewBox: '0 0 16 16'")
    expect(SIDEBAR_SOURCE).toContain("'stroke-width': '1.4'")
    expect(SIDEBAR_SOURCE).toContain("M1.5 8h13")
    expect(SIDEBAR_SOURCE).toContain("M4 8V4.5h8V8M6 8v3.5h6")
    expect(SIDEBAR_SOURCE.match(/createElementNS\(SVG_NS, 'circle'\)/g)).toHaveLength(3)
    expect(SIDEBAR_CSS).toContain('[class*=\'sidebarNarrow\']')
    expect(SIDEBAR_CSS).toContain('@media (max-width: 640px)')
    expect(SIDEBAR_CSS).toContain('justify-content: center')
  })

  it('uses one plugin-specific DOM idempotency key', () => {
    expect(SIDEBAR_ENTRY_SELECTOR).toBe('[data-agent-sidecar-sidebar-entry]')
    expect(hasSidebarEntry({
      querySelector: (selector) => selector === SIDEBAR_ENTRY_SELECTOR ? {} : null,
    })).toBe(true)
    expect(hasSidebarEntry({ querySelector: () => null })).toBe(false)
  })

  it('does not treat the idempotency attribute alone as ownership', () => {
    const attributes = new Map([['data-agent-sidecar-sidebar-entry', '']])
    expect(isOwnedSidebarEntry({
      tagName: 'BUTTON',
      getAttribute: (name: string) => attributes.get(name) ?? null,
      querySelector: () => null,
    })).toBe(false)
  })

  it('is safe to mount and dispose during SSR', () => {
    expect(typeof document).toBe('undefined')
    let opened = false
    const dispose = mountSidebarEntry(() => {
      opened = true
      return true
    }, {
      label: 'Agent Center',
      accessibilityLabel: 'Open Agent Center',
      subscribe: () => () => {},
    })
    expect(opened).toBe(false)
    expect(() => dispose()).not.toThrow()
  })

  it('applies initial copy, follows updates, and unsubscribes on cleanup', () => {
    let current = {
      label: 'Agent Center',
      accessibilityLabel: 'Open Agent Center',
    }
    const listeners = new Set<() => void>()
    let unsubscribeCalls = 0
    const copy: SidebarEntryCopyPort = {
      get label() { return current.label },
      get accessibilityLabel() { return current.accessibilityLabel },
      subscribe: (listener) => {
        listeners.add(listener)
        return () => {
          unsubscribeCalls += 1
          listeners.delete(listener)
        }
      },
    }
    const attributes = new Map<string, string>()
    const target = {
      button: {
        title: '',
        setAttribute: (name: string, value: string) => { attributes.set(name, value) },
      },
      label: { textContent: null as string | null },
    }

    const dispose = bindSidebarEntryCopy(target, copy)
    expect(target.label.textContent).toBe('Agent Center')
    expect(attributes.get('aria-label')).toBe('Open Agent Center')
    expect(target.button.title).toBe('Open Agent Center')

    current = { label: 'Agent 中心', accessibilityLabel: '打开 Agent 中心' }
    for (const listener of [...listeners]) listener()
    expect(target.label.textContent).toBe('Agent 中心')
    expect(attributes.get('aria-label')).toBe('打开 Agent 中心')
    expect(target.button.title).toBe('打开 Agent 中心')

    dispose()
    current = { label: 'ignored', accessibilityLabel: 'ignored' }
    for (const listener of [...listeners]) listener()
    expect(unsubscribeCalls).toBe(1)
    expect(target.label.textContent).toBe('Agent 中心')
  })

  it('moves binding ownership without letting old cleanup remove the row', () => {
    const attributes = new Map<string, string>()
    const remove = vi.fn()
    const target = {
      button: {
        title: '',
        setAttribute: (name: string, value: string) => { attributes.set(name, value) },
        remove,
      },
      label: { textContent: null as string | null },
    }
    const oldUnsubscribe = vi.fn()
    const newUnsubscribe = vi.fn()
    const oldObserver = { disconnect: vi.fn() }
    const newObserver = { disconnect: vi.fn() }
    const oldOpen = vi.fn(() => true)
    const newOpen = vi.fn(() => true)

    const oldDispose = bindSidebarEntryOwner(
      target,
      {},
      oldOpen,
      {
        label: 'Old Center',
        accessibilityLabel: 'Open old center',
        subscribe: () => oldUnsubscribe,
      },
      () => oldObserver,
    )
    expect(isOwnedSidebarEntry(target.button)).toBe(true)
    const newDispose = bindSidebarEntryOwner(
      target,
      {},
      newOpen,
      {
        label: 'New Center',
        accessibilityLabel: 'Open new center',
        subscribe: () => newUnsubscribe,
      },
      () => newObserver,
    )

    expect(oldObserver.disconnect).toHaveBeenCalledOnce()
    expect(oldUnsubscribe).toHaveBeenCalledOnce()
    expect(target.label.textContent).toBe('New Center')
    openBoundSidebarEntry(target.button)
    expect(oldOpen).not.toHaveBeenCalled()
    expect(newOpen).toHaveBeenCalledOnce()

    oldDispose()
    expect(remove).not.toHaveBeenCalled()
    expect(newUnsubscribe).not.toHaveBeenCalled()
    openBoundSidebarEntry(target.button)
    expect(newOpen).toHaveBeenCalledTimes(2)

    newDispose()
    expect(newObserver.disconnect).toHaveBeenCalledOnce()
    expect(newUnsubscribe).toHaveBeenCalledOnce()
    expect(remove).toHaveBeenCalledOnce()
  })

  it('leaves a foreign collision entirely untouched and warns only once', () => {
    const label = { textContent: 'Foreign Center' }
    const attributes = new Map([
      ['data-agent-sidecar-sidebar-entry', ''],
      ['aria-label', 'Foreign label'],
      ['data-owner', 'another-plugin'],
    ])
    const setAttribute = vi.fn((name: string, value: string) => {
      attributes.set(name, value)
    })
    const candidate = {
      tagName: 'BUTTON',
      title: 'Foreign title',
      textContent: 'Foreign text',
      getAttribute: (name: string) => attributes.get(name) ?? null,
      setAttribute,
      querySelector: vi.fn(() => label),
      lastElementChild: label,
      addEventListener: vi.fn(),
      remove: vi.fn(),
    }
    const createElement = vi.fn()
    vi.stubGlobal('document', {
      querySelector: vi.fn(() => candidate),
      createElement,
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const subscribe = vi.fn(() => vi.fn())
    const openCenter = vi.fn(() => true)
    const copy = {
      label: 'Agent Center',
      accessibilityLabel: 'Open Agent Center',
      subscribe,
    }
    const beforeAttributes = [...attributes]

    try {
      const firstDispose = mountSidebarEntry(openCenter, copy)
      const secondDispose = mountSidebarEntry(openCenter, copy)
      firstDispose()
      secondDispose()

      expect(candidate.title).toBe('Foreign title')
      expect(candidate.textContent).toBe('Foreign text')
      expect(label.textContent).toBe('Foreign Center')
      expect([...attributes]).toEqual(beforeAttributes)
      expect(setAttribute).not.toHaveBeenCalled()
      expect(candidate.addEventListener).not.toHaveBeenCalled()
      expect(candidate.remove).not.toHaveBeenCalled()
      expect(createElement).not.toHaveBeenCalled()
      expect(subscribe).not.toHaveBeenCalled()
      expect(openCenter).not.toHaveBeenCalled()
      expect(warn).toHaveBeenCalledOnce()
      expect(warn.mock.calls[0]?.[0]).toContain('foreign')
    } finally {
      warn.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('replaces a structural legacy entry so stale click listeners cannot survive', () => {
    const candidate = legacyButton()
    candidate.title = 'Legacy title'
    candidate.className = 'legacy classes'
    const staleOpen = vi.fn()
    candidate.addEventListener('click', staleOpen)
    const dom = stubSidebarDocument(candidate)
    const unsubscribe = vi.fn()
    const subscribe = vi.fn(() => unsubscribe)
    const openCenter = vi.fn(() => true)

    try {
      const dispose = mountSidebarEntry(openCenter, {
        label: 'Agent Center',
        accessibilityLabel: 'Open Agent Center',
        subscribe,
      })
      const replacement = dom.current()
      expect(replacement).not.toBe(candidate)
      expect(candidate.cloneCalls).toBe(1)
      expect(candidate.replacement).toBe(replacement)
      expect(replacement.className).toBe('legacy classes')
      expect(replacement.attributes.get('data-preserved')).toBe('yes')
      expect(replacement.label.textContent).toBe('Agent Center')
      expect(replacement.attributes.get('aria-label')).toBe('Open Agent Center')
      expect(replacement.title).toBe('Open Agent Center')
      expect(subscribe).toHaveBeenCalledOnce()
      replacement.click()
      expect(staleOpen).not.toHaveBeenCalled()
      expect(openCenter).toHaveBeenCalledOnce()

      dispose()
      expect(unsubscribe).toHaveBeenCalledOnce()
      expect(candidate.remove).not.toHaveBeenCalled()
      expect(replacement.remove).toHaveBeenCalledOnce()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('adopts a current dispatcher in place and routes clicks only to the latest owner', () => {
    const candidate = legacyButton()
    candidate.addEventListener('click', vi.fn())
    const dom = stubSidebarDocument(candidate)
    const firstOpen = vi.fn(() => true)
    const secondOpen = vi.fn(() => true)
    const copy = {
      label: 'Agent Center',
      accessibilityLabel: 'Open Agent Center',
      subscribe: () => vi.fn(),
    }

    try {
      const firstDispose = mountSidebarEntry(firstOpen, copy)
      const current = dom.current()
      const secondDispose = mountSidebarEntry(secondOpen, copy)

      expect(dom.current()).toBe(current)
      expect(current.cloneCalls).toBe(0)
      current.click()
      expect(firstOpen).not.toHaveBeenCalled()
      expect(secondOpen).toHaveBeenCalledOnce()

      firstDispose()
      expect(current.remove).not.toHaveBeenCalled()
      current.click()
      expect(secondOpen).toHaveBeenCalledTimes(2)

      secondDispose()
      expect(current.remove).toHaveBeenCalledOnce()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('clones a bound transition entry missing its dispatcher and strands the old observer', () => {
    const observers: Array<{ callback: () => void; disconnect: ReturnType<typeof vi.fn> }> = []
    vi.stubGlobal('MutationObserver', class {
      readonly record: { callback: () => void; disconnect: ReturnType<typeof vi.fn> }
      constructor(callback: () => void) {
        this.record = { callback, disconnect: vi.fn() }
        observers.push(this.record)
      }
      observe(): void {}
      disconnect(): void { this.record.disconnect() }
    })
    const dom = stubSidebarDocument(legacyButton())
    const copy = {
      label: 'Agent Center',
      accessibilityLabel: 'Open Agent Center',
      subscribe: () => vi.fn(),
    }

    try {
      const oldDispose = mountSidebarEntry(vi.fn(() => true), copy)
      const oldEntry = dom.current()
      delete (oldEntry as unknown as Record<PropertyKey, unknown>)[ENTRY_DISPATCHER]
      const newOpen = vi.fn(() => true)
      const newDispose = mountSidebarEntry(newOpen, copy)
      const replacement = dom.current()

      expect(replacement).not.toBe(oldEntry)
      expect(oldEntry.cloneCalls).toBe(1)
      dom.querySelector.mockClear()
      observers[0]?.callback()
      expect(dom.querySelector.mock.calls).toEqual([[SIDEBAR_ENTRY_SELECTOR]])
      expect(oldEntry.isConnected).toBe(false)

      oldDispose()
      expect(replacement.remove).not.toHaveBeenCalled()
      replacement.click()
      expect(newOpen).toHaveBeenCalledOnce()
      newDispose()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('fails closed when a legacy entry cannot be cloned and replaced', () => {
    const candidate = legacyButton() as unknown as Record<string, unknown>
    candidate.cloneNode = undefined
    candidate.replaceWith = undefined
    const setAttribute = vi.spyOn(candidate as unknown as FakeButton, 'setAttribute')
    vi.stubGlobal('document', { querySelector: vi.fn(() => candidate) })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const subscribe = vi.fn(() => vi.fn())

    try {
      const dispose = mountSidebarEntry(vi.fn(() => true), {
        label: 'Agent Center',
        accessibilityLabel: 'Open Agent Center',
        subscribe,
      })
      dispose()
      expect(setAttribute).not.toHaveBeenCalled()
      expect(subscribe).not.toHaveBeenCalled()
      expect(warn).toHaveBeenCalledOnce()
    } finally {
      warn.mockRestore()
      vi.unstubAllGlobals()
    }
  })
})
