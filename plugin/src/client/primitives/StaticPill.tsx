import { Pill } from '@deepseek-ai/dsh-client-ui-primitives'
import type { HTMLAttributes, ReactElement } from 'react'

export type StaticPillProps = HTMLAttributes<HTMLSpanElement>

/**
 * Preserve native span attributes around the rc.2 Pill static branch, which
 * currently omits its rest props. The inner primitive remains the sole owner
 * of the DSH pill visuals.
 */
export function StaticPill({
  className,
  children,
  ...rest
}: StaticPillProps): ReactElement {
  return (
    <span className={className} {...rest}>
      <Pill>{children}</Pill>
    </span>
  )
}
