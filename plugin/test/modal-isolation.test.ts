import { afterEach, describe, expect, it, vi } from 'vitest'
import { attachModalIsolation } from '../src/client/navigation/modal-isolation.ts'

const DIALOG_SELECTOR = '[role="dialog"][aria-modal="true"]'
const HIDDEN_SELECTOR = '[inert],[aria-hidden="true"]'

class FakeElement {
  readonly attributes = new Map<string, string>()
  readonly children: FakeElement[] = []
  parentElement: FakeElement | null = null
  hidden = false
  isConnected = false
  tabIndex = -1
  focusable = false

  constructor(
    readonly name: string,
    private readonly ownerDocument: FakeDocument,
  ) {}

  get inert(): boolean {
    return this.hasAttribute('inert')
  }

  set inert(value: boolean) {
    if (value) this.setAttribute('inert', '')
    else this.removeAttribute('inert')
  }

  get parentNode(): FakeElement | null {
    return this.parentElement
  }

  appendChild(child: FakeElement): FakeElement {
    child.parentElement?.detachChild(child)
    this.children.push(child)
    child.parentElement = this
    child.setConnected(this.isConnected)
    return child
  }

  remove(): void {
    this.parentElement?.detachChild(this)
  }

  private detachChild(child: FakeElement): void {
    const index = this.children.indexOf(child)
    if (index >= 0) this.children.splice(index, 1)
    child.parentElement = null
    child.setConnected(false)
  }

  setConnected(connected: boolean): void {
    this.isConnected = connected
    for (const child of this.children) child.setConnected(connected)
  }

  contains(node: unknown): boolean {
    if (node === this) return true
    return this.children.some(child => child.contains(node))
  }

  hasAttribute(name: string): boolean {
    return this.attributes.has(name)
  }

  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null
  }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value)
  }

  removeAttribute(name: string): void {
    this.attributes.delete(name)
  }

  private matches(selector: string): boolean {
    if (selector === DIALOG_SELECTOR) {
      return this.getAttribute('role') === 'dialog' &&
        this.getAttribute('aria-modal') === 'true'
    }
    if (selector === HIDDEN_SELECTOR) {
      return this.hasAttribute('inert') || this.getAttribute('aria-hidden') === 'true'
    }
    return this.focusable
  }

  closest<T>(selector: string): T | null {
    let current: FakeElement | null = this
    while (current !== null) {
      if (current.matches(selector)) return current as T
      current = current.parentElement
    }
    return null
  }

  querySelectorAll<T>(selector: string): T[] {
    const matches: T[] = []
    const visit = (element: FakeElement): void => {
      for (const child of element.children) {
        if (child.matches(selector)) matches.push(child as T)
        visit(child)
      }
    }
    visit(this)
    return matches
  }

  getClientRects(): Array<Record<string, never>> {
    return [{}]
  }

  focus(): void {
    if (!this.isConnected) return
    this.ownerDocument.activeElement = this
    this.ownerDocument.dispatch('focusin', { target: this })
  }
}

class FakeDocument {
  readonly listeners = new Map<string, Set<(event: any) => void>>()
  readonly body = new FakeElement('body', this)
  activeElement: FakeElement | null = null

  constructor() {
    this.body.setConnected(true)
  }

  addEventListener(type: string, listener: (event: any) => void): void {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  removeEventListener(type: string, listener: (event: any) => void): void {
    this.listeners.get(type)?.delete(listener)
  }

  dispatch(type: string, event: any): void {
    for (const listener of [...this.listeners.get(type) ?? []]) listener(event)
  }
}

type FakeMutation = {
  type: 'attributes' | 'childList'
  target?: FakeElement
  attributeName?: string | null
  oldValue?: string | null
  addedNodes?: FakeElement[]
  removedNodes?: FakeElement[]
}

function installDom(): {
  document: FakeDocument
  element: (name: string) => FakeElement
  dialog: (name: string) => { dialog: FakeElement; surface: FakeElement }
  button: (name: string) => FakeElement
  flushMutation: (...mutations: FakeMutation[]) => void
  queueMutation: (...mutations: FakeMutation[]) => void
  flushFrames: () => void
} {
  const fakeDocument = new FakeDocument()
  const observers: Array<{
    active: boolean
    callback: (mutations: FakeMutation[]) => void
    pending: FakeMutation[]
  }> = []
  let nextFrame = 1
  const frames = new Map<number, () => void>()

  class FakeMutationObserver {
    readonly record: (typeof observers)[number]

    constructor(callback: (mutations: FakeMutation[]) => void) {
      this.record = { active: true, callback, pending: [] }
      observers.push(this.record)
    }

    observe(): void {
      this.record.active = true
    }

    disconnect(): void {
      this.record.active = false
      this.record.pending = []
    }

    takeRecords(): FakeMutation[] {
      const records = this.record.pending
      this.record.pending = []
      return records
    }
  }

  vi.stubGlobal('HTMLElement', FakeElement)
  vi.stubGlobal('document', fakeDocument)
  vi.stubGlobal('window', {
    MutationObserver: FakeMutationObserver,
    requestAnimationFrame: (callback: () => void) => {
      const frame = nextFrame++
      frames.set(frame, callback)
      return frame
    },
    cancelAnimationFrame: (frame: number) => {
      frames.delete(frame)
    },
  })

  const element = (name: string): FakeElement => new FakeElement(name, fakeDocument)
  return {
    document: fakeDocument,
    element,
    dialog: (name: string) => {
      const modal = element(name)
      modal.setAttribute('role', 'dialog')
      modal.setAttribute('aria-modal', 'true')
      const surface = element(`${name}-surface`)
      modal.appendChild(surface)
      return { dialog: modal, surface }
    },
    button: (name: string) => {
      const button = element(name)
      button.focusable = true
      button.tabIndex = 0
      return button
    },
    flushMutation: (...mutations: FakeMutation[]) => {
      const records = mutations.length > 0
        ? mutations
        : [{ type: 'childList' as const }]
      for (const observer of observers) {
        if (observer.active) observer.callback(records)
      }
    },
    queueMutation: (...mutations: FakeMutation[]) => {
      for (const observer of observers) {
        if (observer.active) observer.pending.push(...mutations)
      }
    },
    flushFrames: () => {
      const queued = [...frames.values()]
      frames.clear()
      for (const callback of queued) callback()
    },
  }
}

function ref(surface: FakeElement): { current: HTMLElement } {
  return { current: surface as unknown as HTMLElement }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('modal isolation', () => {
  it('isolates lower dialogs in the same body branch and skips hidden dialogs', () => {
    const dom = installDom()
    const page = dom.element('page')
    const modalBranch = dom.element('modal-branch')
    const outer = dom.dialog('outer')
    const nested = dom.dialog('nested')
    const hidden = dom.dialog('hidden-unrelated')
    hidden.dialog.setAttribute('aria-hidden', 'true')
    modalBranch.appendChild(outer.dialog)
    modalBranch.appendChild(nested.dialog)
    modalBranch.appendChild(hidden.dialog)
    dom.document.body.appendChild(page)
    dom.document.body.appendChild(modalBranch)

    const dispose = attachModalIsolation(ref(outer.surface))

    expect(page.getAttribute('inert')).toBe('')
    expect(modalBranch.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.getAttribute('inert')).toBe('')
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')
    expect(nested.dialog.hasAttribute('inert')).toBe(false)
    expect(nested.dialog.hasAttribute('aria-hidden')).toBe(false)
    expect(hidden.dialog.hasAttribute('inert')).toBe(false)
    expect(hidden.dialog.getAttribute('aria-hidden')).toBe('true')

    dispose()
    expect(page.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.hasAttribute('aria-hidden')).toBe(false)
    expect(hidden.dialog.getAttribute('aria-hidden')).toBe('true')
  })

  it('keeps body-branch isolation when a nested portal uses another branch', () => {
    const dom = installDom()
    const page = dom.element('page')
    const outerBranch = dom.element('outer-branch')
    const nestedBranch = dom.element('nested-branch')
    const outer = dom.dialog('outer')
    const nested = dom.dialog('nested')
    outerBranch.appendChild(outer.dialog)
    nestedBranch.appendChild(nested.dialog)
    dom.document.body.appendChild(page)
    dom.document.body.appendChild(outerBranch)
    dom.document.body.appendChild(nestedBranch)

    const dispose = attachModalIsolation(ref(outer.surface))

    expect(page.getAttribute('inert')).toBe('')
    expect(outerBranch.getAttribute('inert')).toBe('')
    expect(nestedBranch.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.getAttribute('inert')).toBe('')
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')
    expect(nested.dialog.hasAttribute('inert')).toBe(false)

    nestedBranch.remove()
    dom.flushMutation()
    expect(outerBranch.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.hasAttribute('aria-hidden')).toBe(false)

    dispose()
  })

  it('tracks three portal layers and restores a removed middle layer out of order', () => {
    const dom = installDom()
    const page = dom.element('page')
    const sharedBranch = dom.element('shared-branch')
    const outer = dom.dialog('outer')
    const first = dom.dialog('first-nested')
    const secondBranch = dom.element('second-branch')
    const second = dom.dialog('second-nested')
    const outerButton = dom.button('outer-button')
    const firstButton = dom.button('first-button')
    outer.dialog.appendChild(outerButton)
    first.dialog.appendChild(firstButton)
    sharedBranch.appendChild(outer.dialog)
    dom.document.body.appendChild(page)
    dom.document.body.appendChild(sharedBranch)
    const dispose = attachModalIsolation(ref(outer.surface))

    outerButton.focus()
    sharedBranch.appendChild(first.dialog)
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(firstButton)
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')

    firstButton.focus()
    secondBranch.appendChild(second.dialog)
    dom.document.body.appendChild(secondBranch)
    dom.flushMutation()
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')
    expect(first.dialog.getAttribute('aria-hidden')).toBe('true')
    expect(second.dialog.hasAttribute('inert')).toBe(false)

    first.dialog.remove()
    dom.flushMutation()
    expect(first.dialog.hasAttribute('inert')).toBe(false)
    expect(first.dialog.hasAttribute('aria-hidden')).toBe(false)
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')
    expect(second.dialog.hasAttribute('inert')).toBe(false)

    secondBranch.remove()
    dom.flushMutation()
    dom.flushFrames()
    expect(outer.dialog.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.hasAttribute('aria-hidden')).toBe(false)
    expect(dom.document.activeElement).toBe(outerButton)

    dispose()
  })

  it('restores exact original attributes without claiming existing host inertness', () => {
    const dom = installDom()
    const page = dom.element('page')
    page.setAttribute('inert', 'host-inert')
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const nested = dom.dialog('nested')
    outer.dialog.setAttribute('aria-hidden', 'false')
    branch.appendChild(outer.dialog)
    branch.appendChild(nested.dialog)
    dom.document.body.appendChild(page)
    dom.document.body.appendChild(branch)

    const dispose = attachModalIsolation(ref(outer.surface))
    expect(page.getAttribute('inert')).toBe('host-inert')
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')

    dispose()
    expect(page.getAttribute('inert')).toBe('host-inert')
    expect(outer.dialog.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('false')
  })

  it('does not overwrite host attribute mutations after isolation', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const nested = dom.dialog('nested')
    branch.appendChild(outer.dialog)
    branch.appendChild(nested.dialog)
    dom.document.body.appendChild(branch)
    const dispose = attachModalIsolation(ref(outer.surface))

    outer.dialog.setAttribute('inert', 'host-inert')
    outer.dialog.setAttribute('aria-hidden', 'host-hidden')
    dom.flushMutation(
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: '',
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: 'true',
      },
    )
    dispose()

    expect(outer.dialog.getAttribute('inert')).toBe('host-inert')
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('host-hidden')
  })

  it('honors queued same-value host writes during synchronous disposal', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const nested = dom.dialog('nested')
    branch.appendChild(outer.dialog)
    branch.appendChild(nested.dialog)
    dom.document.body.appendChild(branch)
    const dispose = attachModalIsolation(ref(outer.surface))

    outer.dialog.setAttribute('inert', '')
    outer.dialog.setAttribute('aria-hidden', 'true')
    dom.queueMutation(
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: '',
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: 'true',
      },
    )
    dispose()

    expect(outer.dialog.getAttribute('inert')).toBe('')
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')
  })

  it('hands ownership across HMR and ignores out-of-order disposal', () => {
    const dom = installDom()
    const external = dom.button('external')
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const nested = dom.dialog('nested')
    branch.appendChild(outer.dialog)
    branch.appendChild(nested.dialog)
    dom.document.body.appendChild(external)
    dom.document.body.appendChild(branch)
    external.focus()

    const disposeFirst = attachModalIsolation(ref(outer.surface))
    const disposeSecond = attachModalIsolation(ref(outer.surface))
    const disposeLatest = attachModalIsolation(ref(outer.surface))
    disposeSecond()
    disposeFirst()
    expect(outer.dialog.getAttribute('inert')).toBe('')
    expect(outer.dialog.getAttribute('aria-hidden')).toBe('true')

    disposeLatest()
    expect(outer.dialog.hasAttribute('inert')).toBe(false)
    expect(outer.dialog.hasAttribute('aria-hidden')).toBe(false)
    expect(dom.document.activeElement).toBe(external)
  })

  it('drains pending same-node lifecycle records before HMR handoff', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const outerClose = dom.button('outer-close')
    const oldOpener = dom.button('old-opener')
    const newOpener = dom.button('new-opener')
    outer.dialog.appendChild(outerClose)
    outer.dialog.appendChild(oldOpener)
    outer.dialog.appendChild(newOpener)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(branch)
    const disposeFirst = attachModalIsolation(ref(outer.surface))
    dom.flushFrames()

    const nested = dom.dialog('nested')
    const nestedFirst = dom.button('nested-first')
    nested.dialog.appendChild(nestedFirst)
    oldOpener.focus()
    branch.appendChild(nested.dialog)
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)
    const oldFocus = vi.spyOn(oldOpener, 'focus')

    nested.dialog.remove()
    newOpener.focus()
    branch.appendChild(nested.dialog)
    dom.queueMutation(
      {
        type: 'childList',
        target: branch,
        removedNodes: [nested.dialog],
        addedNodes: [],
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: null,
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: null,
      },
      {
        type: 'childList',
        target: branch,
        removedNodes: [],
        addedNodes: [nested.dialog],
      },
    )

    const disposeLatest = attachModalIsolation(ref(outer.surface))
    disposeFirst()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)

    nested.dialog.remove()
    dom.flushMutation()
    dom.flushFrames()

    expect(dom.document.activeElement).toBe(newOpener)
    expect(oldFocus).not.toHaveBeenCalled()
    disposeLatest()
  })

  it('preserves focus established before fallback frames but restores preferred focus', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const launcher = dom.button('launcher')
    const detail = dom.button('detail')
    outer.dialog.appendChild(launcher)
    outer.dialog.appendChild(detail)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(branch)

    const dispose = attachModalIsolation(ref(outer.surface))
    detail.focus()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(detail)

    launcher.focus()
    const nested = dom.dialog('nested')
    const nestedFirst = dom.button('nested-first')
    nested.dialog.appendChild(nestedFirst)
    branch.appendChild(nested.dialog)
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)

    nested.dialog.remove()
    dom.flushMutation()
    detail.focus()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(launcher)

    dispose()
  })

  it('keeps a queued nested opener across owned restore mutations', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const outerClose = dom.button('outer-close')
    const injectOpener = dom.button('inject-opener')
    outer.dialog.appendChild(outerClose)
    outer.dialog.appendChild(injectOpener)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(branch)
    const dispose = attachModalIsolation(ref(outer.surface))
    dom.flushFrames()

    injectOpener.focus()
    const nested = dom.dialog('nested')
    const nestedFirst = dom.button('nested-first')
    nested.dialog.appendChild(nestedFirst)
    branch.appendChild(nested.dialog)
    dom.flushMutation()
    dom.flushMutation(
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: null,
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: null,
      },
    )
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)

    nested.dialog.remove()
    dom.flushMutation()
    dom.flushMutation(
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: '',
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: 'true',
      },
    )
    dom.flushFrames()

    expect(dom.document.activeElement).toBe(injectOpener)
    dispose()
  })

  it('falls back when a queued nested opener disconnects before the focus frame', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const outerClose = dom.button('outer-close')
    const injectOpener = dom.button('inject-opener')
    outer.dialog.appendChild(outerClose)
    outer.dialog.appendChild(injectOpener)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(branch)
    const dispose = attachModalIsolation(ref(outer.surface))
    dom.flushFrames()

    injectOpener.focus()
    const nested = dom.dialog('nested')
    const nestedFirst = dom.button('nested-first')
    nested.dialog.appendChild(nestedFirst)
    branch.appendChild(nested.dialog)
    dom.flushMutation()
    dom.flushFrames()

    nested.dialog.remove()
    dom.flushMutation()
    injectOpener.remove()
    dom.flushFrames()

    expect(dom.document.activeElement).toBe(outerClose)
    dispose()
  })

  it('treats a known dialog removed then reinserted in one batch as new', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const outerClose = dom.button('outer-close')
    const oldOpener = dom.button('old-opener')
    const newOpener = dom.button('new-opener')
    outer.dialog.appendChild(outerClose)
    outer.dialog.appendChild(oldOpener)
    outer.dialog.appendChild(newOpener)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(branch)
    const dispose = attachModalIsolation(ref(outer.surface))
    dom.flushFrames()

    const nested = dom.dialog('nested')
    const nestedFirst = dom.button('nested-first')
    nested.dialog.appendChild(nestedFirst)
    oldOpener.focus()
    branch.appendChild(nested.dialog)
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)
    const oldFocus = vi.spyOn(oldOpener, 'focus')

    nested.dialog.remove()
    newOpener.focus()
    branch.appendChild(nested.dialog)
    dom.flushMutation(
      {
        type: 'childList',
        target: branch,
        removedNodes: [nested.dialog],
        addedNodes: [],
      },
      {
        type: 'childList',
        target: branch,
        removedNodes: [],
        addedNodes: [nested.dialog],
      },
    )
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)

    nested.dialog.remove()
    dom.flushMutation({
      type: 'childList',
      target: branch,
      removedNodes: [nested.dialog],
      addedNodes: [],
    })
    dom.flushFrames()

    expect(dom.document.activeElement).toBe(newOpener)
    expect(oldFocus).not.toHaveBeenCalled()
    dispose()
  })

  it('keeps new lifecycle focus through deep rapid attribute callbacks', () => {
    const dom = installDom()
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const outerOpener = dom.button('outer-opener')
    outer.dialog.appendChild(outerOpener)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(branch)
    const dispose = attachModalIsolation(ref(outer.surface))
    dom.flushFrames()

    const first = dom.dialog('first-nested')
    const firstOpener = dom.button('first-opener')
    const replacementOpener = dom.button('replacement-opener')
    first.dialog.appendChild(firstOpener)
    first.dialog.appendChild(replacementOpener)
    outerOpener.focus()
    branch.appendChild(first.dialog)
    dom.flushMutation()
    dom.flushFrames()

    const second = dom.dialog('second-nested')
    const secondFirst = dom.button('second-first')
    second.dialog.appendChild(secondFirst)
    firstOpener.focus()
    branch.appendChild(second.dialog)
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(secondFirst)

    second.dialog.remove()
    replacementOpener.focus()
    branch.appendChild(second.dialog)
    dom.flushMutation(
      {
        type: 'childList',
        target: branch,
        removedNodes: [second.dialog],
        addedNodes: [],
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: null,
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: null,
      },
      {
        type: 'attributes',
        target: first.dialog,
        attributeName: 'inert',
        oldValue: null,
      },
      {
        type: 'attributes',
        target: first.dialog,
        attributeName: 'aria-hidden',
        oldValue: null,
      },
      {
        type: 'childList',
        target: branch,
        removedNodes: [],
        addedNodes: [second.dialog],
      },
    )
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(secondFirst)

    second.dialog.remove()
    dom.flushMutation()
    dom.flushMutation(
      {
        type: 'attributes',
        target: first.dialog,
        attributeName: 'inert',
        oldValue: '',
      },
      {
        type: 'attributes',
        target: first.dialog,
        attributeName: 'aria-hidden',
        oldValue: 'true',
      },
    )
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(replacementOpener)

    first.dialog.remove()
    dom.flushMutation()
    dom.flushMutation(
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'inert',
        oldValue: '',
      },
      {
        type: 'attributes',
        target: outer.dialog,
        attributeName: 'aria-hidden',
        oldValue: 'true',
      },
    )
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(outerOpener)
    dispose()
  })

  it('keeps focus trapped and restores nested then external focus across two Escapes', () => {
    const dom = installDom()
    const external = dom.button('external')
    const branch = dom.element('branch')
    const outer = dom.dialog('outer')
    const launcher = dom.button('launcher')
    const outerLast = dom.button('outer-last')
    outer.dialog.appendChild(launcher)
    outer.dialog.appendChild(outerLast)
    branch.appendChild(outer.dialog)
    dom.document.body.appendChild(external)
    dom.document.body.appendChild(branch)
    external.focus()
    const dispose = attachModalIsolation(ref(outer.surface))
    dom.flushFrames()
    launcher.focus()

    const nested = dom.dialog('nested')
    const nestedFirst = dom.button('nested-first')
    const nestedLast = dom.button('nested-last')
    nested.dialog.appendChild(nestedFirst)
    nested.dialog.appendChild(nestedLast)
    branch.appendChild(nested.dialog)
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(nestedFirst)

    nestedLast.focus()
    const tab = {
      key: 'Tab',
      defaultPrevented: false,
      shiftKey: false,
      preventDefault() { this.defaultPrevented = true },
    }
    dom.document.dispatch('keydown', tab)
    expect(tab.defaultPrevented).toBe(true)
    expect(dom.document.activeElement).toBe(nestedFirst)

    const firstEscape = {
      key: 'Escape',
      defaultPrevented: false,
      shiftKey: false,
      preventDefault() { this.defaultPrevented = true },
    }
    dom.document.dispatch('keydown', firstEscape)
    expect(firstEscape.defaultPrevented).toBe(false)
    nested.dialog.remove()
    dom.flushMutation()
    dom.flushFrames()
    expect(dom.document.activeElement).toBe(launcher)

    const secondEscape = {
      key: 'Escape',
      defaultPrevented: false,
      shiftKey: false,
      preventDefault() { this.defaultPrevented = true },
    }
    dom.document.dispatch('keydown', secondEscape)
    expect(secondEscape.defaultPrevented).toBe(false)
    branch.remove()
    dom.flushMutation()
    expect(dom.document.activeElement).toBe(external)

    dispose()
    expect(dom.document.activeElement).toBe(external)
  })
})
