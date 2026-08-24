/**
 * Agent Sidecar — browser client half (hello-slot smoke).
 *
 * Registers one "Sidecar" tab into the `conversation.view` slot ring (the
 * official "multiple views per session" extension seat; registration shape
 * follows the dsh-project-kanban / dsh-agent-teams precedents:
 * `ctx.slots.inject(key, () => ctx.slots.register(options, Component))`).
 *
 * No JSX on purpose: the smoke bundle needs nothing beyond
 * `React.createElement`, keeping the emitted factory minimal for the
 * byte-format comparison against the web-ui blueprint artifacts.
 */

import { createElement as h } from 'react'
import type { ReactElement } from 'react'
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
// Type-only: merges the 'conversation.view' declaration into SlotMap.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'

export const name = 'agent-sidecar'

/** The slot registry is the only service the hello tab consumes. */
export const inject = ['slots']

/** Static hello pane proving the client bundle materialized and mounted. */
function SidecarHelloView(): ReactElement {
  return h(
    'div',
    {
      style: {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100%',
        minHeight: 240,
        fontSize: 15,
        color: 'var(--dsw-alias-label-primary)',
      },
      'data-testid': 'agent-sidecar-hello',
    },
    'hello from agent-sidecar',
  )
}

/**
 * Mount the hello tab. `slots.inject` defers until the conversation body
 * entry declares the `conversation.view` ring, and the returned disposer
 * rides the caller's fiber, so plugin unload removes the tab.
 * @param ctx - browser plugin context handed by the client loader.
 */
export function apply(ctx: ClientContext): void {
  ctx.slots.inject('conversation.view', () => ctx.slots.register({
    name: 'conversation.view',
    id: 'agent-sidecar',
    order: 30,
    label: 'Sidecar',
  }, SidecarHelloView))
}
