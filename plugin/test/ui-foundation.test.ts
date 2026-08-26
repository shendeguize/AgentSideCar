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
const AGSC_THEME_CSS = readFileSync(
  new URL('../src/client/theme/agsc.module.css', import.meta.url),
  'utf8',
)
const INJECT_CSS = readFileSync(
  new URL('../src/client/inject/inject.module.css', import.meta.url),
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
  {
    name: 'inject panel',
    source: INJECT_CSS,
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
  '--dsw-alias-interactive-bg-hover-danger',
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

type Rgba = readonly [red: number, green: number, blue: number, alpha: number]
type HostTokenFixture = Record<string, string>

// Resolved from dsh-client-ui-theme's default official-tokens-v1 light/dark themes.
const HOST_TOKEN_FIXTURES: Record<'light' | 'dark', HostTokenFixture> = {
  light: {
    '--dsw-alias-bg-layer-3': '#ffffff',
    '--dsw-alias-label-primary': '#0f1115',
    '--dsw-alias-label-secondary': '#61666b',
    '--dsw-alias-interactive-bg-hover-danger': '#ec13130d',
  },
  dark: {
    '--dsw-alias-bg-layer-3': '#353638',
    '--dsw-alias-label-primary': '#f9fafb',
    '--dsw-alias-label-secondary': '#cfd3d6',
    '--dsw-alias-interactive-bg-hover-danger': '#f25a5a26',
  },
}

function cssDeclaration(source: string, selector: string, property: string): string {
  const css = source.replace(/\/\*[\s\S]*?\*\//g, '')
  const propertyPattern = new RegExp(
    `(?:^|;)\\s*${property.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*:\\s*([^;]+)`,
  )

  for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = (rule[1] ?? '').split(',').map(value => value.trim())
    if (!selectors.includes(selector)) continue
    const value = propertyPattern.exec(rule[2] ?? '')?.[1]?.trim()
    if (value !== undefined) return value
  }
  throw new Error(`Missing ${property} declaration for ${selector}`)
}

function resolveColor(value: string, fixture: HostTokenFixture): Rgba {
  const token = /^var\((--[a-z0-9-]+)\)$/i.exec(value)?.[1]
  if (token !== undefined) {
    const resolved = token.startsWith('--agsc-')
      ? cssDeclaration(AGSC_THEME_CSS, '.root', token)
      : fixture[token]
    if (resolved === undefined) throw new Error(`Missing host token fixture: ${token}`)
    return resolveColor(resolved, fixture)
  }
  if (value === 'transparent') return [0, 0, 0, 0]

  const hex = /^#([\da-f]{6})([\da-f]{2})?$/i.exec(value)
  if (hex === null) throw new Error(`Unsupported fixture color: ${value}`)
  const rgb = hex[1] ?? ''
  return [
    Number.parseInt(rgb.slice(0, 2), 16),
    Number.parseInt(rgb.slice(2, 4), 16),
    Number.parseInt(rgb.slice(4, 6), 16),
    Number.parseInt(hex[2] ?? 'ff', 16) / 255,
  ]
}

function composite(foreground: Rgba, background: Rgba): Rgba {
  const alpha = foreground[3] + background[3] * (1 - foreground[3])
  const channel = (index: 0 | 1 | 2): number => (
    (foreground[index] * foreground[3]
      + background[index] * background[3] * (1 - foreground[3])) / alpha
  )
  return [channel(0), channel(1), channel(2), alpha]
}

function relativeLuminance(color: Rgba): number {
  const linear = color.slice(0, 3).map((channel) => {
    const normalized = channel / 255
    return normalized <= 0.04045
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * (linear[0] ?? 0) + 0.7152 * (linear[1] ?? 0)
    + 0.0722 * (linear[2] ?? 0)
}

function computedContrast(
  foreground: string,
  background: string,
  surface: string,
  fixture: HostTokenFixture,
): number {
  const surfaceColor = resolveColor(surface, fixture)
  const backgroundColor = composite(resolveColor(background, fixture), surfaceColor)
  const foregroundColor = composite(resolveColor(foreground, fixture), backgroundColor)
  const lighter = Math.max(relativeLuminance(foregroundColor), relativeLuminance(backgroundColor))
  const darker = Math.min(relativeLuminance(foregroundColor), relativeLuminance(backgroundColor))
  return (lighter + 0.05) / (darker + 0.05)
}

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

  it('keeps injection text on readable semantic tokens without literal colors', () => {
    expect(cssDeclaration(INJECT_CSS, '.planKey', 'color')).toBe('var(--agsc-fg-secondary)')
    expect(cssDeclaration(INJECT_CSS, '.observedNote', 'color'))
      .toBe('var(--agsc-fg-secondary)')
    expect(cssDeclaration(INJECT_CSS, '.btnDanger', 'border'))
      .toBe('1px solid var(--agsc-err)')
    expect(cssDeclaration(INJECT_CSS, '.btnDanger', 'color')).toBe('var(--agsc-fg)')
    expect(cssDeclaration(INJECT_CSS, '.btnDanger:hover:not(:disabled)', 'background'))
      .toBe('var(--dsw-alias-interactive-bg-hover-danger)')
    expect(cssDeclaration(INJECT_CSS, '.btnDanger:hover:not(:disabled)', 'color'))
      .toBe('var(--agsc-fg)')
    expect(cssDeclaration(INJECT_CSS, '.btnDanger:disabled', 'opacity')).toBe('0.4')
    expect(INJECT_CSS).not.toMatch(/#[\da-f]{3,8}\b|(?:rgb|hsl)a?\(/i)
  })

  it.each(Object.entries(HOST_TOKEN_FIXTURES))(
    'meets normal-text AA for default %s plan and danger states',
    (_theme, fixture) => {
      const raisedSurface = 'var(--agsc-bg-raised)'
      const scenarios = {
        plan: {
          foreground: cssDeclaration(INJECT_CSS, '.planKey', 'color'),
          background: cssDeclaration(INJECT_CSS, '.planBox', 'background'),
        },
        rest: {
          foreground: cssDeclaration(INJECT_CSS, '.btnDanger', 'color'),
          background: cssDeclaration(INJECT_CSS, '.btnDanger', 'background'),
        },
        hover: {
          foreground: cssDeclaration(INJECT_CSS, '.btnDanger:hover:not(:disabled)', 'color'),
          background: cssDeclaration(INJECT_CSS, '.btnDanger:hover:not(:disabled)', 'background'),
        },
      }

      for (const [state, colors] of Object.entries(scenarios)) {
        expect(
          computedContrast(colors.foreground, colors.background, raisedSurface, fixture),
          state,
        ).toBeGreaterThanOrEqual(4.5)
      }
    },
  )
})
