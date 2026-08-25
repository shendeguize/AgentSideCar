/**
 * Footer status widget (design §5.1 view 4, `sidebar.footer.action` slot).
 *
 * Presentation-only: connection state and working count arrive as props
 * (derive them with `deriveWidgetConnection` / `countWorking` from
 * `board/logic.ts`); the click callback is wired to the board tab by the
 * integration layer.
 *
 * Quiet by design: at zero working sessions only the connection dot shows,
 * no counter, no animation — the DOM stays stable so the footer never
 * flickers or shifts (task spec: 零数据时安静).
 */

import type { CSSProperties, ReactElement } from 'react'
import { widgetTitle, type WidgetConnection } from './board/logic.ts'
import { surfaceProps } from './theme/parts.ts'

export interface SidecarWidgetProps {
  connection: WidgetConnection
  workingCount: number
  /** Opens the board tab; wired by the integration layer. */
  onOpen?: () => void
}

const DOT_COLORS: Record<WidgetConnection, string> = {
  ok: 'var(--agsc-ok)',
  degraded: 'var(--agsc-warn)',
  off: 'var(--agsc-fg-dimmed)',
}

const rootStyle: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  padding: '2px 6px',
  border: 'none',
  borderRadius: 6,
  background: 'transparent',
  font: 'inherit',
  color: 'var(--agsc-fg-secondary)',
}

const countStyle: CSSProperties = {
  fontSize: 11,
  fontVariantNumeric: 'tabular-nums',
  lineHeight: '14px',
  whiteSpace: 'nowrap',
}

/**
 * Connection dot + working-session counter (e.g. `▸2`) for the footer.
 *
 * Without `onOpen` the widget remains an inert status `<span>` so button
 * semantics are only emitted for an actual control.
 */
export function SidecarWidget(props: SidecarWidgetProps): ReactElement {
  const title = widgetTitle(props.connection, props.workingCount)
  const body = (
    <>
      <span
        aria-hidden
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          flex: 'none',
          background: DOT_COLORS[props.connection],
        }}
      />
      {props.workingCount > 0 && (
        <span style={countStyle} data-testid="agent-sidecar-widget-count">
          {`▸${props.workingCount}`}
        </span>
      )}
    </>
  )
  if (props.onOpen === undefined) {
    return (
      <span
        {...surfaceProps('footer-widget')}
        style={rootStyle}
        title={title}
        aria-label={title}
        role="status"
        data-testid="agent-sidecar-widget"
        data-connection={props.connection}
      >
        {body}
      </span>
    )
  }
  return (
    <button
      {...surfaceProps('footer-widget')}
      type="button"
      style={{ ...rootStyle, cursor: 'pointer' }}
      title={title}
      aria-label={title}
      onClick={props.onOpen}
      data-testid="agent-sidecar-widget"
      data-connection={props.connection}
    >
      {body}
    </button>
  )
}
