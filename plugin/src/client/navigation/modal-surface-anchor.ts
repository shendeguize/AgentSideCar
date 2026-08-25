import { useEffect, useLayoutEffect } from 'react'
import type { RefObject } from 'react'
import type { SurfaceProps } from '../theme/parts.ts'

const DIALOG_SELECTOR = '[role="dialog"][aria-modal="true"]'
const MODAL_SURFACE_OWNER = Symbol.for(
  '@shendeguize/dsh-agent-sidecar/modal-surface-anchor-owner',
)

type ModalSurfaceAttributes = Pick<
  SurfaceProps,
  'data-dsh-plugin' | 'data-dsh-part'
>

export interface ModalSurfaceAnchorTarget {
  setAttribute(name: string, value: string): void
  removeAttribute(name: string): void
}

/** Use a pre-paint effect in the browser without warning during SSR. */
export function selectModalSurfaceAnchorEffect(
  hasDocument: boolean,
): typeof useEffect {
  return hasDocument ? useLayoutEffect : useEffect
}

const useIsomorphicLayoutEffect = selectModalSurfaceAnchorEffect(
  typeof document !== 'undefined',
)

function ownerSlot(target: ModalSurfaceAnchorTarget): Record<PropertyKey, unknown> {
  return target as unknown as Record<PropertyKey, unknown>
}

/** Attach the public surface attributes with latest-owner-safe cleanup. */
export function attachModalSurfaceAnchor(
  target: ModalSurfaceAnchorTarget | null,
  attributes: ModalSurfaceAttributes,
): () => void {
  if (target === null) return () => {}

  const owner = Symbol('modal-surface-anchor')
  const slot = ownerSlot(target)
  slot[MODAL_SURFACE_OWNER] = owner
  target.setAttribute('data-dsh-plugin', attributes['data-dsh-plugin'])
  target.setAttribute('data-dsh-part', attributes['data-dsh-part'])

  return () => {
    if (slot[MODAL_SURFACE_OWNER] !== owner) return
    target.removeAttribute('data-dsh-plugin')
    target.removeAttribute('data-dsh-part')
    delete slot[MODAL_SURFACE_OWNER]
  }
}

/** Commit the public anchor onto the official Modal's real dialog element. */
export function useModalSurfaceAnchor(
  open: boolean,
  surfaceRef: RefObject<HTMLElement>,
  attributes: ModalSurfaceAttributes,
): void {
  useIsomorphicLayoutEffect(() => {
    if (!open || typeof document === 'undefined') return
    const dialog = surfaceRef.current?.closest<HTMLElement>(DIALOG_SELECTOR) ?? null
    return attachModalSurfaceAnchor(dialog, attributes)
  }, [
    open,
    surfaceRef,
    attributes['data-dsh-plugin'],
    attributes['data-dsh-part'],
  ])
}
