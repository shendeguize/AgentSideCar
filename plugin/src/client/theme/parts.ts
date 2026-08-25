import theme from './agsc.module.css'

/** Stable owner value for Agent Sidecar skin anchors. */
export const PLUGIN_DOM_ID = 'agent-sidecar'

/**
 * Closed public registry of bare `data-dsh-part` values.
 * Adding an entry changes the theming contract and requires tests + docs.
 */
export const SIDECAR_PARTS = Object.freeze([
  'board',
  'board-toolbar',
  'board-card',
  'project-view',
  'detail',
  'timeline',
  'inject-panel',
  'analysis-panel',
  'dsh-tools',
  'footer-widget',
  'sidebar-entry',
  'settings-card',
  'overlay',
  'sidebar-tab',
] as const)

export type SidecarPart = (typeof SIDECAR_PARTS)[number]

export interface SurfaceProps {
  readonly className: string
  readonly 'data-dsh-plugin': typeof PLUGIN_DOM_ID
  readonly 'data-dsh-part': SidecarPart
}

function requireThemeClass(name: string): string {
  const className = theme[name]
  if (className === undefined || className.length === 0) {
    throw new Error(`Agent Sidecar theme is missing its ${name} class`)
  }
  return className
}

const rootClassName = requireThemeClass('root')

/**
 * Returns the complete, spreadable contract for one mounted surface.
 * This keeps hashed CSS classes internal while exposing stable DSH anchors.
 */
export function surfaceProps(part: SidecarPart, className?: string): SurfaceProps {
  const localClassName = className?.trim()
  return {
    className: localClassName ? `${rootClassName} ${localClassName}` : rootClassName,
    'data-dsh-plugin': PLUGIN_DOM_ID,
    'data-dsh-part': part,
  }
}
