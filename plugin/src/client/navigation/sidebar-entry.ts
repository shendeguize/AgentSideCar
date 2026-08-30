/** First-class Agent Center row inserted after the shell's New Session row. */
import type { CenterNavigation } from './center.ts'
import css from './sidebar-entry.module.css'
import { PLUGIN_DOM_ID, surfaceProps } from '../theme/parts.ts'

export const SIDEBAR_ENTRY_SELECTOR = '[data-agent-sidecar-sidebar-entry]'
const ENTRY_ATTRIBUTE = 'data-agent-sidecar-sidebar-entry'
const LABEL_ATTRIBUTE = 'data-agent-sidecar-sidebar-entry-label'
const SVG_NS = 'http://www.w3.org/2000/svg'
const ENTRY_BINDING = Symbol.for('@shendeguize/dsh-agent-sidecar/sidebar-entry-binding')
const ENTRY_BRAND = Symbol.for('@shendeguize/dsh-agent-sidecar/sidebar-entry-brand')
const ENTRY_DISPATCHER = Symbol.for('@shendeguize/dsh-agent-sidecar/sidebar-entry-dispatcher')
const warnedForeignEntries = new WeakSet<object>()

interface QueryScope { querySelector(selector: string): unknown | null }
export interface SidebarEntryCopyPort {
  readonly label: string
  readonly accessibilityLabel: string
  subscribe(listener: () => void): () => void
}
export interface SidebarEntryCopyTarget {
  readonly button: {
    title: string
    setAttribute(name: string, value: string): void
  }
  readonly label: { textContent: string | null }
}
interface SidebarEntryElements extends SidebarEntryCopyTarget { readonly button: HTMLButtonElement }
interface SidebarEntryObserver { disconnect(): void }
interface SidebarEntryBinding {
  readonly owner: object
  readonly openCenter: CenterNavigation
  stopCopy: () => void
  observer: SidebarEntryObserver | null
}
export interface SidebarEntryOwnerTarget extends SidebarEntryCopyTarget {
  readonly button: SidebarEntryCopyTarget['button'] & { remove(): void }
}
/** Narrow, DOM-independent idempotency check used by mount and unit tests. */
export function hasSidebarEntry(scope: QueryScope): boolean {
  return scope.querySelector(SIDEBAR_ENTRY_SELECTOR) !== null
}
export function applyCopy(target: SidebarEntryCopyTarget, copy: SidebarEntryCopyPort): void {
  target.label.textContent = copy.label
  target.button.setAttribute('aria-label', copy.accessibilityLabel)
  target.button.title = copy.accessibilityLabel
}

export function bindSidebarEntryCopy(
  target: SidebarEntryCopyTarget,
  copy: SidebarEntryCopyPort,
): () => void {
  const update = (): void => { applyCopy(target, copy) }
  update()
  return copy.subscribe(update)
}

function bindingOf(button: object): SidebarEntryBinding | undefined {
  return (button as Record<PropertyKey, unknown>)[ENTRY_BINDING] as SidebarEntryBinding | undefined
}
function hasCurrentDispatcher(button: object): boolean {
  const record = button as Record<PropertyKey, unknown>
  return (record[ENTRY_BRAND] === true || record[ENTRY_BINDING] !== undefined)
    && record[ENTRY_DISPATCHER] === true
}
/** A pure, DOM-independent ownership check; the idempotency attribute alone is foreign. */
export function isOwnedSidebarEntry(candidate: unknown): boolean {
  if (candidate === null
    || (typeof candidate !== 'object' && typeof candidate !== 'function')) return false
  const record = candidate as Record<PropertyKey, unknown>
  if (record[ENTRY_BRAND] === true || record[ENTRY_BINDING] !== undefined) return true
  const element = candidate as {
    tagName?: unknown
    getAttribute?: (name: string) => string | null
    querySelector?: (selector: string) => unknown | null
  }
  return element.tagName === 'BUTTON'
    && element.getAttribute?.('data-dsh-plugin') === PLUGIN_DOM_ID
    && element.getAttribute?.('data-dsh-part') === 'sidebar-entry'
    && element.querySelector?.(`[${LABEL_ATTRIBUTE}]`) != null
}

function brandSidebarEntry(button: object): void {
  (button as Record<PropertyKey, unknown>)[ENTRY_BRAND] = true
}
function installClickDispatcher(button: HTMLButtonElement): void {
  const record = button as unknown as Record<PropertyKey, unknown>
  if (record[ENTRY_DISPATCHER] === true) return
  button.addEventListener('click', () => { openBoundSidebarEntry(button) })
  record[ENTRY_DISPATCHER] = true
}
function once(dispose: () => void): () => void {
  let active = true
  return () => {
    if (!active) return
    active = false
    dispose()
  }
}
/** Invoke the latest cross-bundle binding, never a captured HMR closure. */
export function openBoundSidebarEntry(button: object): void {
  try {
    bindingOf(button)?.openCenter()
  } catch {
    // Navigation is a best-effort bridge to host-owned tab DOM.
  }
}
/** Latest-owner-wins binding over one shared DOM button. */
export function bindSidebarEntryOwner(
  target: SidebarEntryOwnerTarget,
  owner: object,
  openCenter: CenterNavigation,
  copy: SidebarEntryCopyPort,
  startObserver: () => SidebarEntryObserver | null,
): () => void {
  const previous = bindingOf(target.button)
  previous?.observer?.disconnect()
  previous?.stopCopy()

  const binding: SidebarEntryBinding = {
    owner,
    openCenter,
    stopCopy: () => {},
    observer: null,
  }
  const sharedButton = target.button as unknown as Record<PropertyKey, unknown>
  sharedButton[ENTRY_BINDING] = binding
  binding.stopCopy = once(bindSidebarEntryCopy(target, copy))
  binding.observer = startObserver()

  return () => {
    binding.observer?.disconnect()
    if (bindingOf(target.button)?.owner !== owner) return
    binding.stopCopy()
    delete sharedButton[ENTRY_BINDING]
    target.button.remove()
  }
}

function createIcon(): SVGSVGElement {
  const icon = document.createElementNS(SVG_NS, 'svg')
  for (const [name, value] of Object.entries({
    viewBox: '0 0 16 16',
    width: '16',
    height: '16',
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': '1.4',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
    focusable: 'false',
  })) {
    icon.setAttribute(name, value)
  }
  // The icon is intentionally a small line diagram rather than a
  // decorative glyph: one trunk, two parallel bypass branches, and
  // observation nodes where the routes can be read.
  const trunk = document.createElementNS(SVG_NS, 'path')
  trunk.setAttribute('d', 'M1.5 8h13')
  const bypass = document.createElementNS(SVG_NS, 'path')
  bypass.setAttribute('d', 'M4 8V4.5h8V8M6 8v3.5h6')
  const upperNode = document.createElementNS(SVG_NS, 'circle')
  upperNode.setAttribute('cx', '4')
  upperNode.setAttribute('cy', '4.5')
  upperNode.setAttribute('r', '1.15')
  const trunkNode = document.createElementNS(SVG_NS, 'circle')
  trunkNode.setAttribute('cx', '8')
  trunkNode.setAttribute('cy', '8')
  trunkNode.setAttribute('r', '1.15')
  const lowerNode = document.createElementNS(SVG_NS, 'circle')
  lowerNode.setAttribute('cx', '6')
  lowerNode.setAttribute('cy', '11.5')
  lowerNode.setAttribute('r', '1.15')
  icon.append(trunk, bypass, upperNode, trunkNode, lowerNode)
  return icon
}

function createEntry(): SidebarEntryElements {
  const entry = document.createElement('button')
  brandSidebarEntry(entry)
  const surface = surfaceProps('sidebar-entry', css.entry)
  entry.type = 'button'
  entry.className = surface.className
  entry.setAttribute(ENTRY_ATTRIBUTE, '')
  entry.setAttribute('data-dsh-plugin', surface['data-dsh-plugin'])
  entry.setAttribute('data-dsh-part', surface['data-dsh-part'])

  const icon = document.createElement('span')
  icon.className = css.entryIcon ?? ''
  icon.setAttribute('aria-hidden', 'true')
  icon.appendChild(createIcon())
  const label = document.createElement('span')
  label.className = css.entryLabel ?? ''
  label.setAttribute(LABEL_ATTRIBUTE, '')
  entry.append(icon, label)
  installClickDispatcher(entry)
  return { button: entry, label }
}

function existingEntry(button: HTMLButtonElement): SidebarEntryElements | null {
  const label = button.querySelector<HTMLElement>(`[${LABEL_ATTRIBUTE}]`)
    ?? (button.lastElementChild as HTMLElement | null)
  return label === null ? null : { button, label }
}

function rejectLegacyEntry(): null {
  console.warn('[agent-sidecar] Sidebar legacy entry cannot be safely replaced; leaving it untouched.')
  return null
}

function replaceLegacyEntry(button: HTMLButtonElement): SidebarEntryElements | null {
  if (typeof button.cloneNode !== 'function' || typeof button.replaceWith !== 'function') {
    return rejectLegacyEntry()
  }
  let clone: HTMLButtonElement
  try {
    clone = button.cloneNode(true) as HTMLButtonElement
  } catch {
    return rejectLegacyEntry()
  }
  const elements = clone.tagName === 'BUTTON' && typeof clone.addEventListener === 'function'
    ? existingEntry(clone)
    : null
  if (elements === null) return rejectLegacyEntry()
  try {
    button.replaceWith(clone)
  } catch {
    return rejectLegacyEntry()
  }
  brandSidebarEntry(clone)
  installClickDispatcher(clone)
  return elements
}

function sidebarRoot(): HTMLElement | null {
  const column = document.querySelector<HTMLElement>(
    '[data-pane="sidebar"], [class*="sidebarCol"]',
  )
  if (column === null) return null
  const logoOwner = column.querySelector<HTMLElement>('[class*="logoRow"]')?.parentElement
  return logoOwner ?? (column.firstElementChild as HTMLElement | null)
}

function newSessionRow(root: HTMLElement): Element | null {
  const nested = root.querySelector<HTMLButtonElement>('button[class*="newSession"]')
  const button = nested ?? Array.from(root.children).find((child) => child.tagName === 'BUTTON')
  if (button === undefined) return null
  const row = button.closest('[class*="logoRow"]')
  if (row !== null && row.parentElement === root) return row
  return button.parentElement === root ? button : null
}

function placeEntry(entry: HTMLButtonElement): boolean {
  const root = sidebarRoot()
  if (root === null) return false
  const anchor = newSessionRow(root)
  if (anchor === null) return false
  if (entry.parentElement !== root || anchor.nextElementSibling !== entry) {
    root.insertBefore(entry, anchor.nextElementSibling)
  }
  return true
}

/**
 * Wait for the sidebar, restore the row after React rebuilds, and return full
 * cleanup. Overlapping applies synchronously adopt the existing row.
 */
export function mountSidebarEntry(
  openCenter: CenterNavigation,
  copy: SidebarEntryCopyPort,
): () => void {
  if (typeof document === 'undefined') return () => {}
  const candidate = document.querySelector<HTMLButtonElement>(SIDEBAR_ENTRY_SELECTOR)
  if (candidate !== null && !isOwnedSidebarEntry(candidate)) {
    if (!warnedForeignEntries.has(candidate)) {
      warnedForeignEntries.add(candidate)
      console.warn('[agent-sidecar] Sidebar entry collision: refusing to modify a foreign '
        + SIDEBAR_ENTRY_SELECTOR + ' node.')
    }
    return () => {}
  }
  const elements = candidate === null
    ? createEntry()
    : hasCurrentDispatcher(candidate)
      ? existingEntry(candidate)
      : replaceLegacyEntry(candidate)
  if (elements === null) return () => {}
  brandSidebarEntry(elements.button)
  const entry = elements.button
  const owner = {}
  let disposed = false
  const ensurePlaced = (): void => {
    if (disposed) return
    if (entry.isConnected) return
    const existing = document.querySelector(SIDEBAR_ENTRY_SELECTOR)
    if (existing !== null && existing !== entry) return
    placeEntry(entry)
  }
  ensurePlaced()
  const disposeBinding = bindSidebarEntryOwner(
    elements,
    owner,
    openCenter,
    copy,
    () => {
      if (typeof MutationObserver === 'undefined') return null
      const observer = new MutationObserver(ensurePlaced)
      observer.observe(document.body, { childList: true, subtree: true })
      return observer
    },
  )
  return () => {
    if (disposed) return
    disposed = true
    disposeBinding()
  }
}
