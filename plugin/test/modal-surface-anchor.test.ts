import { readFileSync } from 'node:fs'
import { useEffect, useLayoutEffect } from 'react'
import { describe, expect, it } from 'vitest'
import {
  attachModalSurfaceAnchor,
  selectModalSurfaceAnchorEffect,
  type ModalSurfaceAnchorTarget,
} from '../src/client/navigation/modal-surface-anchor.ts'
import { surfaceProps } from '../src/client/theme/parts.ts'

const CENTER_OVERLAY_SOURCE = readFileSync(
  new URL('../src/client/navigation/CenterOverlay.tsx', import.meta.url),
  'utf8',
)

class FakeTarget implements ModalSurfaceAnchorTarget {
  readonly attributes = new Map<string, string>()
  className = 'host modal'

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value)
  }

  removeAttribute(name: string): void {
    this.attributes.delete(name)
  }
}

describe('modal surface anchor', () => {
  it('selects a pre-paint effect only when a document is available', () => {
    expect(selectModalSurfaceAnchorEffect(true)).toBe(useLayoutEffect)
    expect(selectModalSurfaceAnchorEffect(false)).toBe(useEffect)
  })

  it('sets both public attributes on the supplied dialog node', () => {
    const dialog = new FakeTarget()
    const surface = surfaceProps('overlay', 'dialog')

    attachModalSurfaceAnchor(dialog, surface)

    expect(dialog.attributes.get('data-dsh-plugin')).toBe(surface['data-dsh-plugin'])
    expect(dialog.attributes.get('data-dsh-part')).toBe(surface['data-dsh-part'])
  })

  it('is a no-op when the committed child has no dialog ancestor', () => {
    const surface = surfaceProps('overlay', 'dialog')
    expect(() => attachModalSurfaceAnchor(null, surface)()).not.toThrow()
  })

  it('lets the latest owner take over and guards both cleanup paths', () => {
    const dialog = new FakeTarget()
    dialog.attributes.set('aria-label', 'Host dialog')
    const surface = surfaceProps('overlay', 'dialog')

    const cleanupOldOwner = attachModalSurfaceAnchor(dialog, surface)
    const cleanupLatestOwner = attachModalSurfaceAnchor(dialog, surface)

    cleanupOldOwner()
    expect(dialog.attributes.get('data-dsh-plugin')).toBe(surface['data-dsh-plugin'])
    expect(dialog.attributes.get('data-dsh-part')).toBe(surface['data-dsh-part'])

    cleanupLatestOwner()
    expect(dialog.attributes.has('data-dsh-plugin')).toBe(false)
    expect(dialog.attributes.has('data-dsh-part')).toBe(false)
    expect(dialog.attributes.get('aria-label')).toBe('Host dialog')
    expect(dialog.className).toBe('host modal')
  })

  it('keeps surfaceProps as the class and attribute source for the hook', () => {
    expect(CENTER_OVERLAY_SOURCE).toContain(
      "const surface = surfaceProps('overlay', css['dialog'])",
    )
    expect(CENTER_OVERLAY_SOURCE).toContain(
      'useModalSurfaceAnchor(props.open, surfaceRef, surface)',
    )
    expect(CENTER_OVERLAY_SOURCE).toContain('className={surface.className}')
    expect(CENTER_OVERLAY_SOURCE).not.toContain('data-dsh-plugin={surface')
    expect(CENTER_OVERLAY_SOURCE).not.toContain('data-dsh-part={surface')
  })
})
