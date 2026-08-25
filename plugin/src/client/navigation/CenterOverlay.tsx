import { Modal } from '@deepseek-ai/dsh-client-ui-primitives'
import { useRef } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { surfaceProps } from '../theme/parts.ts'
import css from './center-overlay.module.css'
import { useModalIsolation } from './modal-isolation.ts'
import { useModalSurfaceAnchor } from './modal-surface-anchor.ts'

export interface CenterOverlayProps {
  open: boolean
  onClose: () => void
  title: string
  closeLabel: string
  children?: ReactNode
}

/** Pure Agent Center presentation over the shell's frame-wide overlay seat. */
export function CenterOverlay(props: CenterOverlayProps): ReactElement {
  const surfaceRef = useRef<HTMLDivElement>(null)
  const surface = surfaceProps('overlay', css['dialog'])
  useModalIsolation(props.open, surfaceRef)
  useModalSurfaceAnchor(props.open, surfaceRef, surface)
  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title={props.title}
      closeLabel={props.closeLabel}
      className={surface.className}
      contentClassName={css['content']}
    >
      <div
        ref={surfaceRef}
        className={css['surface']}
      >
        {props.children}
      </div>
    </Modal>
  )
}
