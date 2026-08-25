import { useEffect } from 'react'
import type { RefObject } from 'react'
const DIALOG_SELECTOR = '[role="dialog"][aria-modal="true"]'
const FOCUSABLE_SELECTOR = [
  'a[href],area[href],button:not([disabled])',
  'input:not([disabled]):not([type="hidden"]),select:not([disabled])',
  'textarea:not([disabled]),iframe,[contenteditable="true"],[tabindex]',
].join(',')
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
type DialogFrame = { dialog: HTMLElement; opener: HTMLElement | null; focus: HTMLElement | null }
/** Isolate the active official Modal, including sibling-portaled nested dialogs. */
export function useModalIsolation(
  open: boolean,
  surfaceRef: RefObject<HTMLElement>,
): void {
  useEffect(() => {
    if (!open || typeof document === 'undefined' || typeof window === 'undefined') return
    const body = document.body
    if (body === null || typeof window.MutationObserver === 'undefined') return
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null
    const originalInert = new Map<HTMLElement, boolean>()
    const dialogStack: DialogFrame[] = []
    let topDialog: HTMLElement | null = null
    let focusFrame: number | null = null
    const getTopDialog = (): HTMLElement | null => {
      const outer = surfaceRef.current?.closest<HTMLElement>(DIALOG_SELECTOR) ?? null
      if (outer === null) return null
      const dialogs = Array.from(body.querySelectorAll<HTMLElement>(DIALOG_SELECTOR))
      const outerIndex = dialogs.indexOf(outer)
      return outerIndex < 0 ? null : dialogs.at(-1) ?? null
    }
    const bodyBranch = (dialog: HTMLElement): HTMLElement | null => {
      let branch: Node = dialog
      while (branch.parentNode !== null && branch.parentNode !== body) {
        branch = branch.parentNode
      }
      return branch instanceof HTMLElement ? branch : null
    }
    const isolateAround = (dialog: HTMLElement | null): void => {
      const activeBranch = dialog === null ? null : bodyBranch(dialog)
      const next = new Set(
        Array.from(body.children)
          .filter((element): element is HTMLElement =>
            element instanceof HTMLElement && element !== activeBranch),
      )
      for (const [element, wasInert] of originalInert) {
        if (next.has(element)) continue
        element.inert = wasInert
        originalInert.delete(element)
      }
      for (const element of next) {
        if (!originalInert.has(element)) originalInert.set(element, element.inert)
        element.inert = true
      }
    }
    const queueFocus = (dialog: HTMLElement, preferred: HTMLElement | null = null): void => {
      if (focusFrame !== null) window.cancelAnimationFrame(focusFrame)
      focusFrame = window.requestAnimationFrame(() => {
        focusFrame = null
        if (getTopDialog() !== dialog) return
        const target = restorableIn(preferred, dialog)
          ? preferred
          : focusableElements(dialog)[0]
        target?.focus({ preventScroll: true })
      })
    }
    const sync = (): void => {
      const next = getTopDialog()
      const changed = next !== topDialog
      let closed: DialogFrame[] = []
      if (changed && next !== null) {
        const index = dialogStack.findIndex(frame => frame.dialog === next)
        if (index < 0) {
          const parent = dialogStack.at(-1)
          const active =
            document.activeElement instanceof HTMLElement ? document.activeElement : null
          const opener = parent !== undefined && restorableIn(active, parent.dialog)
            ? active
            : parent?.focus ?? null
          dialogStack.push({ dialog: next, opener, focus: null })
        } else {
          closed = dialogStack.splice(index + 1)
        }
      }
      topDialog = next
      isolateAround(next)
      const opener = next === null
        ? null
        : closed.reverse().find(frame => restorableIn(frame.opener, next))?.opener ?? null
      if (
        next !== null &&
        (changed || !next.contains(document.activeElement))
      ) {
        queueFocus(next, opener)
      }
    }
    const onFocusIn = (event: FocusEvent): void => {
      if (!(event.target instanceof HTMLElement)) return
      const dialog = event.target.closest<HTMLElement>(DIALOG_SELECTOR)
      const frame = dialogStack.find(item => item.dialog === dialog)
      if (frame !== undefined) frame.focus = event.target
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Tab' || event.defaultPrevented) return
      const dialog = getTopDialog()
      if (dialog === null) return
      if (dialog !== topDialog) sync()

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
    const observer = new window.MutationObserver(sync)
    observer.observe(body, { childList: true, subtree: true })
    document.addEventListener('focusin', onFocusIn)
    document.addEventListener('keydown', onKeyDown, true)
    sync()
    return () => {
      observer.disconnect()
      document.removeEventListener('focusin', onFocusIn)
      document.removeEventListener('keydown', onKeyDown, true)
      if (focusFrame !== null) window.cancelAnimationFrame(focusFrame)
      for (const [element, wasInert] of originalInert) element.inert = wasInert
      if (previousFocus?.isConnected === true) previousFocus.focus({ preventScroll: true })
    }
  }, [open, surfaceRef])
}
