/**
 * Detail-page dispose control: when it is offered at all, and what it says
 * about an attempt that did not end the session.
 *
 * Node environment; the component goes through `renderToStaticMarkup`, so
 * these assertions cover the render-time decisions (offer / hide / copy)
 * rather than click sequences.
 */

import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

vi.mock('@deepseek-ai/dsh-client-ui-primitives', () => ({
  Button: 'button',
  IconChevronDownOutline14: 'svg',
  Input: 'input',
  Modal: 'div',
  Pill: 'span',
  StateDot: 'span',
  writeClipboard: vi.fn(() => Promise.resolve(true)),
}))

const { DetailDisposeButton, disposeFailureKey } = await import(
  '../src/client/detail-view.tsx'
)
const { t } = await import('../src/client/locales/index.ts')

function render(props: { agent: string; capable: boolean }): string {
  return renderToStaticMarkup(createElement(DetailDisposeButton, {
    ...props,
    onDispose: () => Promise.resolve('disposed' as const),
    onDisposed: () => {},
  }))
}

describe('detail dispose control', () => {
  it('is offered only for a dsh session on a capable host', () => {
    expect(render({ agent: 'dsh', capable: true }))
      .toContain('agent-sidecar-detail-dispose')
  })

  it('renders nothing at all when the host cannot dispose', () => {
    // Not disabled — absent. A disabled button sends the operator hunting
    // for a setting, while archiving (which does work) is one click away.
    expect(render({ agent: 'dsh', capable: false })).toBe('')
  })

  it.each(['claude', 'codex', 'kimi', 'copilot', 'cursor'])(
    'renders nothing for a %s session, which has no session to end',
    (agent) => {
      expect(render({ agent, capable: true })).toBe('')
    },
  )

  it('warns before doing anything, and names the irreversibility', () => {
    const html = render({ agent: 'dsh', capable: true })
    expect(html).toContain(t('detail.actions.dispose'))
    // The confirm dialog copy must distinguish this from archiving.
    expect(t('detail.dispose.explain')).toContain('归档')
    expect(t('detail.dispose.explain')).toContain('无法撤销')
  })
})

describe('dispose outcome copy', () => {
  it('says nothing for the two outcomes that mean the session is gone', () => {
    expect(disposeFailureKey('disposed')).toBeNull()
    expect(disposeFailureKey('not_found')).toBeNull()
  })

  it('distinguishes a timeout from a refusal and from an absent capability', () => {
    expect(disposeFailureKey('timeout')).toBe('detail.dispose.outcome.timeout')
    expect(disposeFailureKey('failed')).toBe('detail.dispose.outcome.failed')
    expect(disposeFailureKey('unsupported')).toBe('detail.dispose.outcome.unsupported')
    // A timeout leaves the session possibly alive, so the copy must not
    // read as a settled failure.
    expect(t('detail.dispose.outcome.timeout')).toContain('可能仍在运行')
  })
})
