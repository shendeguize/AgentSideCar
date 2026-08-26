import { useEffect } from 'react'
import type { RefObject } from 'react'

const DIALOG_SELECTOR = '[role="dialog"][aria-modal="true"]'
const MODAL_ISOLATION_STATE = Symbol.for(
  '@shendeguize/dsh-agent-sidecar/modal-isolation-state',
)
const FOCUSABLE_SELECTOR = [
  'a[href],area[href],button:not([disabled])',
  'input:not([disabled]):not([type="hidden"]),select:not([disabled])',
  'textarea:not([disabled]),iframe,[contenteditable="true"],[tabindex]',
].join(',')

type IsolatedAttribute = 'inert' | 'aria-hidden'
type AttributeSnapshot = {
  present: boolean
  value: string | null
}
type AttributeLease = {
  original: AttributeSnapshot
  written: AttributeSnapshot
  generation: number
}
type DialogFrame = {
  dialog: HTMLElement
  opener: HTMLElement | null
  focus: HTMLElement | null
  generation: number
}
type PendingFocus = {
  frame: number
  dialog: HTMLElement
  preferred: HTMLElement | null
  generation: number
  runtimeGeneration: number
}
type ModalIsolationState = {
  body: HTMLElement
  owner: symbol
  generation: number
  surfaceRef: RefObject<HTMLElement>
  previousFocus: HTMLElement | null
  dialogStack: DialogFrame[]
  topDialog: HTMLElement | null
  nextDialogGeneration: number
  leases: Map<HTMLElement, Map<IsolatedAttribute, AttributeLease>>
  contested: Map<HTMLElement, Set<IsolatedAttribute>>
  pendingMutations: Map<HTMLElement, Map<IsolatedAttribute, Array<string | null>>>
  observer: MutationObserver | null
  onFocusIn: ((event: FocusEvent) => void) | null
  onKeyDown: ((event: KeyboardEvent) => void) | null
  pendingFocus: PendingFocus | null
}

function focusableElements(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((element) =>
      element.tabIndex >= 0 &&
      !element.hidden &&
      element.closest('[inert],[aria-hidden="true"]') === null &&
      element.getClientRects().length > 0)
}
function restorableIn(element: HTMLElement | null, dialog: HTMLElement): element is HTMLElement {
  return element?.isConnected === true &&
    dialog.contains(element) &&
    !element.hidden &&
    element.closest('[inert],[aria-hidden="true"]') === null &&
    element.getClientRects().length > 0
}

function activeElement(): HTMLElement | null {
  return document.activeElement instanceof HTMLElement ? document.activeElement : null
}

function stateSlot(target: HTMLElement): Record<PropertyKey, unknown> {
  return target as unknown as Record<PropertyKey, unknown>
}

function snapshotAttribute(
  element: HTMLElement,
  attribute: IsolatedAttribute,
): AttributeSnapshot {
  return {
    present: element.hasAttribute(attribute),
    value: element.getAttribute(attribute),
  }
}

function snapshotsEqual(left: AttributeSnapshot, right: AttributeSnapshot): boolean {
  return left.present === right.present && left.value === right.value
}

function leaseFor(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
): AttributeLease | undefined {
  return state.leases.get(element)?.get(attribute)
}

function ownsAttribute(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
): boolean {
  const lease = leaseFor(state, element, attribute)
  return lease !== undefined &&
    snapshotsEqual(snapshotAttribute(element, attribute), lease.written)
}

function markContested(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
): void {
  const attributes = state.contested.get(element) ?? new Set<IsolatedAttribute>()
  attributes.add(attribute)
  state.contested.set(element, attributes)
  const leases = state.leases.get(element)
  leases?.delete(attribute)
  if (leases?.size === 0) state.leases.delete(element)
}

function auditLeases(state: ModalIsolationState): void {
  for (const [element, leases] of [...state.leases]) {
    for (const [attribute, lease] of [...leases]) {
      if (!snapshotsEqual(snapshotAttribute(element, attribute), lease.written)) {
        markContested(state, element, attribute)
      }
    }
  }
}

function notePendingMutation(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
  oldValue: string | null,
): void {
  const byAttribute = state.pendingMutations.get(element) ??
    new Map<IsolatedAttribute, Array<string | null>>()
  const mutations = byAttribute.get(attribute) ?? []
  mutations.push(oldValue)
  byAttribute.set(attribute, mutations)
  state.pendingMutations.set(element, byAttribute)
}

function consumePendingMutation(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
  oldValue: string | null,
): boolean {
  const byAttribute = state.pendingMutations.get(element)
  const mutations = byAttribute?.get(attribute)
  if (mutations?.[0] !== oldValue) return false
  mutations.shift()
  if (mutations.length === 0) byAttribute?.delete(attribute)
  if (byAttribute?.size === 0) state.pendingMutations.delete(element)
  return true
}

function processAttributeMutations(
  state: ModalIsolationState,
  mutations: MutationRecord[],
): void {
  for (const mutation of mutations) {
    if (
      mutation.type !== 'attributes' ||
      !(mutation.target instanceof HTMLElement) ||
      (mutation.attributeName !== 'inert' && mutation.attributeName !== 'aria-hidden')
    ) {
      continue
    }
    const attribute = mutation.attributeName
    if (consumePendingMutation(state, mutation.target, attribute, mutation.oldValue)) continue
    if (leaseFor(state, mutation.target, attribute) !== undefined) {
      markContested(state, mutation.target, attribute)
    }
  }
}

function reinsertedDialogs(
  state: ModalIsolationState,
  mutations: MutationRecord[],
): Set<HTMLElement> {
  const known = state.dialogStack.map(frame => frame.dialog)
  const removed = new Set<HTMLElement>()
  const reinserted = new Set<HTMLElement>()
  for (const mutation of mutations) {
    if (mutation.type !== 'childList') continue
    const removedNodes = Array.from(mutation.removedNodes ?? [])
    const addedNodes = Array.from(mutation.addedNodes ?? [])
    for (const dialog of known) {
      if (removedNodes.some(node => node === dialog || node.contains(dialog))) {
        removed.add(dialog)
      }
      if (
        removed.has(dialog) &&
        addedNodes.some(node => node === dialog || node.contains(dialog))
      ) {
        reinserted.add(dialog)
      }
    }
  }
  return reinserted
}

function writeOwnedAttribute(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
  value: string,
): void {
  if (state.contested.get(element)?.has(attribute) === true) return
  const existing = leaseFor(state, element, attribute)
  if (existing !== undefined) {
    if (!ownsAttribute(state, element, attribute)) {
      markContested(state, element, attribute)
      return
    }
    existing.generation = state.generation
    return
  }

  const original = snapshotAttribute(element, attribute)
  const intended = { present: true, value }
  if (attribute === 'inert' && (original.present || element.inert)) return
  if (snapshotsEqual(original, intended)) return
  notePendingMutation(state, element, attribute, original.value)
  element.setAttribute(attribute, value)
  const written = snapshotAttribute(element, attribute)
  const leases = state.leases.get(element) ??
    new Map<IsolatedAttribute, AttributeLease>()
  leases.set(attribute, { original, written, generation: state.generation })
  state.leases.set(element, leases)
}

function restoreOwnedAttribute(
  state: ModalIsolationState,
  element: HTMLElement,
  attribute: IsolatedAttribute,
): void {
  const lease = leaseFor(state, element, attribute)
  if (lease !== undefined) {
    if (ownsAttribute(state, element, attribute)) {
      const current = snapshotAttribute(element, attribute)
      notePendingMutation(state, element, attribute, current.value)
      if (lease.original.present) {
        element.setAttribute(attribute, lease.original.value ?? '')
      } else {
        element.removeAttribute(attribute)
      }
    }
    const leases = state.leases.get(element)
    leases?.delete(attribute)
    if (leases?.size === 0) state.leases.delete(element)
  }
  const contested = state.contested.get(element)
  contested?.delete(attribute)
  if (contested?.size === 0) state.contested.delete(element)
}

function syncOwnedAttributes(
  state: ModalIsolationState,
  desired: Map<HTMLElement, Set<IsolatedAttribute>>,
): void {
  for (const [element, leases] of [...state.leases]) {
    for (const attribute of [...leases.keys()]) {
      if (desired.get(element)?.has(attribute) !== true) {
        restoreOwnedAttribute(state, element, attribute)
      }
    }
  }
  for (const [element, attributes] of [...state.contested]) {
    for (const attribute of [...attributes]) {
      if (desired.get(element)?.has(attribute) !== true) {
        restoreOwnedAttribute(state, element, attribute)
      }
    }
  }
  for (const [element, attributes] of desired) {
    for (const attribute of attributes) {
      writeOwnedAttribute(state, element, attribute, attribute === 'inert' ? '' : 'true')
    }
  }
}

function isHiddenOutsideIsolation(
  state: ModalIsolationState,
  element: HTMLElement,
): boolean {
  let current: HTMLElement | null = element
  while (current !== null && current !== state.body) {
    if (current.hidden) return true
    if (
      current.getAttribute('aria-hidden') === 'true' &&
      !ownsAttribute(state, current, 'aria-hidden')
    ) {
      return true
    }
    if (
      (current.hasAttribute('inert') || current.inert) &&
      !ownsAttribute(state, current, 'inert')
    ) {
      return true
    }
    current = current.parentElement
  }
  return false
}

function collectModalDialogs(state: ModalIsolationState): HTMLElement[] {
  const outer = state.surfaceRef.current?.closest<HTMLElement>(DIALOG_SELECTOR) ?? null
  if (
    outer === null ||
    !state.body.contains(outer) ||
    isHiddenOutsideIsolation(state, outer)
  ) {
    return []
  }
  const dialogs = Array.from(
    state.body.querySelectorAll<HTMLElement>(DIALOG_SELECTOR),
  )
  const outerIndex = dialogs.indexOf(outer)
  if (outerIndex < 0) return []
  return dialogs.slice(outerIndex)
    .filter(dialog => !isHiddenOutsideIsolation(state, dialog))
}

function restorableOpenerIn(
  state: ModalIsolationState,
  element: HTMLElement | null,
  dialog: HTMLElement,
): element is HTMLElement {
  return element?.isConnected === true &&
    dialog.contains(element) &&
    !element.hidden &&
    !isHiddenOutsideIsolation(state, element) &&
    element.getClientRects().length > 0
}

function refreshDialogStack(
  state: ModalIsolationState,
  reinserted: Set<HTMLElement> = new Set(),
): { changed: boolean; closed: DialogFrame[] } {
  const previousTop = state.dialogStack.at(-1)
  const dialogs = collectModalDialogs(state)
  const mounted = new Set(dialogs)
  const closed = state.dialogStack.filter(frame =>
    !mounted.has(frame.dialog) || reinserted.has(frame.dialog))
  state.dialogStack = state.dialogStack.filter(frame =>
    mounted.has(frame.dialog) && !reinserted.has(frame.dialog))

  for (const dialog of dialogs) {
    if (state.dialogStack.some(frame => frame.dialog === dialog)) continue
    const parent = state.dialogStack.at(-1)
    const active = activeElement()
    const opener = parent !== undefined &&
      restorableOpenerIn(state, active, parent.dialog)
      ? active
      : reinserted.has(dialog)
        ? null
        : parent?.focus ?? null
    state.nextDialogGeneration += 1
    state.dialogStack.push({
      dialog,
      opener,
      focus: null,
      generation: state.nextDialogGeneration,
    })
  }
  const currentTop = state.dialogStack.at(-1)
  state.topDialog = currentTop?.dialog ?? null
  return { changed: previousTop !== currentTop, closed }
}

function bodyBranch(body: HTMLElement, dialog: HTMLElement): HTMLElement | null {
  let branch: HTMLElement = dialog
  while (branch.parentElement !== null && branch.parentElement !== body) {
    branch = branch.parentElement
  }
  return branch.parentElement === body ? branch : null
}

function desiredIsolation(
  state: ModalIsolationState,
): Map<HTMLElement, Set<IsolatedAttribute>> {
  const desired = new Map<HTMLElement, Set<IsolatedAttribute>>()
  const top = state.topDialog
  if (top === null) return desired

  const add = (element: HTMLElement, attribute: IsolatedAttribute): void => {
    const attributes = desired.get(element) ?? new Set<IsolatedAttribute>()
    attributes.add(attribute)
    desired.set(element, attributes)
  }
  const activeBranch = bodyBranch(state.body, top)
  for (const child of Array.from(state.body.children)) {
    if (child instanceof HTMLElement && child !== activeBranch) add(child, 'inert')
  }
  for (const frame of state.dialogStack) {
    if (frame.dialog === top || frame.dialog.contains(top)) continue
    add(frame.dialog, 'inert')
    add(frame.dialog, 'aria-hidden')
  }
  return desired
}

function queueFocus(
  state: ModalIsolationState,
  dialog: HTMLElement,
  preferred: HTMLElement | null = null,
): void {
  const dialogGeneration = state.dialogStack
    .find(frame => frame.dialog === dialog)
    ?.generation
  if (dialogGeneration === undefined) return
  const pending = state.pendingFocus
  if (
    pending !== null &&
    pending.dialog === dialog &&
    pending.generation === dialogGeneration &&
    pending.runtimeGeneration === state.generation &&
    preferred === null &&
    restorableIn(pending.preferred, dialog)
  ) {
    return
  }
  if (pending !== null) window.cancelAnimationFrame(pending.frame)
  const owner = state.owner
  const request: PendingFocus = {
    frame: 0,
    dialog,
    preferred,
    generation: dialogGeneration,
    runtimeGeneration: state.generation,
  }
  request.frame = window.requestAnimationFrame(() => {
    if (state.pendingFocus !== request) return
    state.pendingFocus = null
    const currentFrame = state.dialogStack.at(-1)
    if (
      state.owner !== owner ||
      state.generation !== request.runtimeGeneration ||
      currentFrame?.dialog !== request.dialog ||
      currentFrame.generation !== request.generation ||
      !request.dialog.isConnected ||
      !state.body.contains(request.dialog)
    ) {
      return
    }
    if (
      request.preferred === null &&
      request.dialog.contains(document.activeElement)
    ) {
      return
    }
    const target = restorableIn(request.preferred, request.dialog)
      ? request.preferred
      : focusableElements(request.dialog)[0]
    target?.focus({ preventScroll: true })
  })
  state.pendingFocus = request
}

function syncIsolation(
  state: ModalIsolationState,
  reinserted: Set<HTMLElement> = new Set(),
  restoreFocus = true,
): void {
  auditLeases(state)
  const { changed, closed } = refreshDialogStack(state, reinserted)
  syncOwnedAttributes(state, desiredIsolation(state))
  if (!restoreFocus) return
  const next = state.topDialog
  const opener = next === null
    ? null
    : [...closed].reverse()
      .find(frame => restorableIn(frame.opener, next))
      ?.opener ?? null
  if (next !== null && (changed || !next.contains(document.activeElement))) {
    queueFocus(state, next, opener)
  } else if (changed && next === null && state.previousFocus?.isConnected === true) {
    state.previousFocus.focus({ preventScroll: true })
  }
}

function processMutationBatch(
  state: ModalIsolationState,
  mutations: MutationRecord[],
  restoreFocus = true,
): void {
  const reinserted = reinsertedDialogs(state, mutations)
  processAttributeMutations(state, mutations)
  syncIsolation(state, reinserted, restoreFocus)
}

function stopRuntime(state: ModalIsolationState): void {
  if (state.observer !== null) {
    const mutations = state.observer.takeRecords()
    state.observer.disconnect()
    if (mutations.length > 0) {
      processMutationBatch(state, mutations, false)
    }
  }
  state.observer = null
  if (state.onFocusIn !== null) {
    document.removeEventListener('focusin', state.onFocusIn)
    state.onFocusIn = null
  }
  if (state.onKeyDown !== null) {
    document.removeEventListener('keydown', state.onKeyDown, true)
    state.onKeyDown = null
  }
  if (state.pendingFocus !== null) {
    window.cancelAnimationFrame(state.pendingFocus.frame)
    state.pendingFocus = null
  }
  state.pendingMutations.clear()
}

function startRuntime(state: ModalIsolationState): void {
  const owner = state.owner
  const generation = state.generation
  const isCurrent = (): boolean =>
    state.owner === owner && state.generation === generation

  state.onFocusIn = (event: FocusEvent): void => {
    if (!isCurrent() || !(event.target instanceof HTMLElement)) return
    const dialog = event.target.closest<HTMLElement>(DIALOG_SELECTOR)
    const frame = state.dialogStack.find(item => item.dialog === dialog)
    if (frame !== undefined) frame.focus = event.target
  }
  state.onKeyDown = (event: KeyboardEvent): void => {
    if (!isCurrent() || event.key !== 'Tab' || event.defaultPrevented) return
    syncIsolation(state)
    const dialog = state.topDialog
    if (dialog === null) return

    const focusable = focusableElements(dialog)
    if (focusable.length === 0) {
      event.preventDefault()
      return
    }
    const activeIndex = focusable.indexOf(document.activeElement as HTMLElement)
    const wrapsBackward = event.shiftKey && activeIndex <= 0
    const wrapsForward = !event.shiftKey && activeIndex === focusable.length - 1
    if (activeIndex < 0 || wrapsBackward || wrapsForward) {
      event.preventDefault()
      const target = event.shiftKey ? focusable.at(-1) : focusable[0]
      target?.focus({ preventScroll: true })
    }
  }
  state.observer = new window.MutationObserver((mutations) => {
    if (!isCurrent()) return
    processMutationBatch(state, mutations)
  })
  state.observer.observe(state.body, {
    attributes: true,
    attributeFilter: ['inert', 'aria-hidden'],
    attributeOldValue: true,
    childList: true,
    subtree: true,
  })
  document.addEventListener('focusin', state.onFocusIn)
  document.addEventListener('keydown', state.onKeyDown, true)
  syncIsolation(state)
}

/** Attach body-branch and stacked-dialog isolation with HMR-safe ownership. */
export function attachModalIsolation(
  surfaceRef: RefObject<HTMLElement>,
): () => void {
  if (
    typeof document === 'undefined' ||
    typeof window === 'undefined' ||
    typeof window.MutationObserver === 'undefined'
  ) {
    return () => {}
  }
  const body = document.body
  if (body === null) return () => {}

  const owner = Symbol('modal-isolation')
  const slot = stateSlot(body)
  const previous = slot[MODAL_ISOLATION_STATE] as ModalIsolationState | undefined
  const state: ModalIsolationState = previous ?? {
    body,
    owner,
    generation: 0,
    surfaceRef,
    previousFocus: activeElement(),
    dialogStack: [],
    topDialog: null,
    nextDialogGeneration: 0,
    leases: new Map(),
    contested: new Map(),
    pendingMutations: new Map(),
    observer: null,
    onFocusIn: null,
    onKeyDown: null,
    pendingFocus: null,
  }
  if (previous !== undefined) stopRuntime(previous)
  if (!Number.isSafeInteger(state.nextDialogGeneration)) {
    state.nextDialogGeneration = 0
  }
  for (const frame of state.dialogStack) {
    if (Number.isSafeInteger(frame.generation)) {
      state.nextDialogGeneration = Math.max(
        state.nextDialogGeneration,
        frame.generation,
      )
    } else {
      state.nextDialogGeneration += 1
      frame.generation = state.nextDialogGeneration
    }
  }
  state.owner = owner
  state.generation += 1
  state.surfaceRef = surfaceRef
  for (const leases of state.leases.values()) {
    for (const lease of leases.values()) lease.generation = state.generation
  }
  slot[MODAL_ISOLATION_STATE] = state
  startRuntime(state)

  return () => {
    if (state.owner !== owner || slot[MODAL_ISOLATION_STATE] !== state) return
    stopRuntime(state)
    auditLeases(state)
    syncOwnedAttributes(state, new Map())
    state.dialogStack = []
    state.topDialog = null
    delete slot[MODAL_ISOLATION_STATE]
    if (state.previousFocus?.isConnected === true) {
      state.previousFocus.focus({ preventScroll: true })
    }
  }
}

/** Isolate the active official Modal, including sibling-portaled nested dialogs. */
export function useModalIsolation(
  open: boolean,
  surfaceRef: RefObject<HTMLElement>,
): void {
  useEffect(() => {
    if (!open) return
    return attachModalIsolation(surfaceRef)
  }, [open, surfaceRef])
}
