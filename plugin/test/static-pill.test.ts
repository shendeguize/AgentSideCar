import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { StaticPill } from '../src/client/primitives/StaticPill.tsx'

vi.mock('@deepseek-ai/dsh-client-ui-primitives', () => ({
  Pill: 'i',
}))

describe('StaticPill', () => {
  it('keeps native attributes and the custom class on its outer span', () => {
    const html = renderToStaticMarkup(createElement(StaticPill, {
      className: 'custom-pill',
      'data-kind': 'current',
      'data-tone': 'success',
      title: 'Observed status',
      role: 'status',
      'data-testid': 'static-pill',
      'aria-label': 'Current session',
      children: 'ready',
    }))
    const outer = html.match(/^<span[^>]*>/)?.[0] ?? ''

    expect(outer).toContain('class="custom-pill"')
    expect(outer).toContain('data-kind="current"')
    expect(outer).toContain('data-tone="success"')
    expect(outer).toContain('title="Observed status"')
    expect(outer).toContain('role="status"')
    expect(outer).toContain('data-testid="static-pill"')
    expect(outer).toContain('aria-label="Current session"')
    expect(html).toContain('<i>ready</i>')
    expect(html.match(/custom-pill/g)).toHaveLength(1)
  })

  it('does not invent interactive semantics for a static pill', () => {
    const html = renderToStaticMarkup(createElement(StaticPill, null, 'static'))

    expect(html).toBe('<span><i>static</i></span>')
    expect(html).not.toContain('<button')
  })
})
