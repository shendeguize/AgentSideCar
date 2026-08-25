import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'
import {
  PLUGIN_DOM_ID,
  SIDECAR_PARTS,
  surfaceProps,
} from '../src/client/theme/parts.ts'

const BOARD_SOURCE = readFileSync(
  new URL('../src/client/board/Board.tsx', import.meta.url),
  'utf8',
)
const DETAIL_VIEW_SOURCE = readFileSync(
  new URL('../src/client/detail-view.tsx', import.meta.url),
  'utf8',
)

const THEME_CSS = [
  {
    name: 'project view',
    source: readFileSync(new URL('../src/client/board/project-view.module.css', import.meta.url), 'utf8'),
  },
  {
    name: 'settings card',
    source: readFileSync(new URL('../src/client/settings-card.module.css', import.meta.url), 'utf8'),
  },
] as const

// Minimal used-token subset of Skin Center's official-tokens-v1 contract.
const OFFICIAL_DSW_TOKEN_ALLOWLIST = new Set([
  '--dsw-alias-bg-base',
  '--dsw-alias-bg-layer-1',
  '--dsw-alias-bg-layer-2',
  '--dsw-alias-bg-layer-3',
  '--dsw-alias-bg-module-platform',
  '--dsw-alias-border-l2',
  '--dsw-alias-brand-primary',
  '--dsw-alias-interactive-bg-hover',
  '--dsw-alias-label-dimmed',
  '--dsw-alias-label-primary',
  '--dsw-alias-label-primary-foreground',
  '--dsw-alias-label-secondary',
  '--dsw-alias-label-tertiary',
  '--dsw-alias-state-error-primary',
  '--dsw-alias-state-error-secondary',
])

const EXPECTED_PARTS = [
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
] as const

describe('Agent Sidecar surface contract', () => {
  it('keeps the public part registry explicit and immutable', () => {
    expect(SIDECAR_PARTS).toEqual(EXPECTED_PARTS)
    expect(Object.isFrozen(SIDECAR_PARTS)).toBe(true)
  })

  it('emits stable DSH attributes and the theme root for every part', () => {
    const rootClassName = surfaceProps('board').className
    expect(rootClassName).not.toBe('')

    for (const part of SIDECAR_PARTS) {
      expect(surfaceProps(part)).toEqual({
        className: rootClassName,
        'data-dsh-plugin': PLUGIN_DOM_ID,
        'data-dsh-part': part,
      })
    }
  })

  it('merges local classes without leaking undefined or blank input', () => {
    const rootClassName = surfaceProps('board').className

    expect(surfaceProps('board', 'board local').className)
      .toBe(`${rootClassName} board local`)
    expect(surfaceProps('board', '  board local  ').className)
      .toBe(`${rootClassName} board local`)
    expect(surfaceProps('board', '   ').className).toBe(rootClassName)
    expect(surfaceProps('board').className).not.toContain('undefined')
  })

  it('routes board toolbar and card markup through the complete surface contract', () => {
    expect(BOARD_SOURCE).toMatch(
      /<header\s+\{\.\.\.surfaceProps\('board-toolbar', styles\['topbar'\]\)\}>/,
    )
    expect(BOARD_SOURCE).toMatch(
      /<article\s+\{\.\.\.surfaceProps\('board-card', styles\['card'\]\)\}/,
    )
    expect(BOARD_SOURCE).not.toMatch(
      /data-dsh-part=["'](?:board-toolbar|board-card)["']/,
    )
  })

  it('routes the detail tools shell through the complete surface contract', () => {
    expect(DETAIL_VIEW_SOURCE).toContain(
      "{...surfaceProps('dsh-tools', css['toolsSection'])}",
    )
    expect(DETAIL_VIEW_SOURCE).not.toMatch(/data-dsh-part=["']dsh-tools["']/)
  })

  it('uses only the official DSH tokens required by theme CSS', () => {
    const knownInvalidTokens = [
      '--dsw-alias-label-error',
      '--dsw-alias-static-white',
    ]

    for (const { source } of THEME_CSS) {
      for (const token of knownInvalidTokens) expect(source).not.toContain(token)
      const tokens = source.match(/--dsw-[a-z0-9-]+/g) ?? []
      expect(tokens.filter(token => !OFFICIAL_DSW_TOKEN_ALLOWLIST.has(token))).toEqual([])
    }

    expect(THEME_CSS.find(css => css.name === 'project view')?.source)
      .toContain('--dsw-alias-label-primary-foreground')
    expect(THEME_CSS.find(css => css.name === 'settings card')?.source)
      .toContain('--dsw-alias-state-error-primary')
  })
})
