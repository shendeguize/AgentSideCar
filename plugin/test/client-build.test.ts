import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { ModuleKind, ScriptTarget, transpileModule } from 'typescript'
import { describe, expect, it } from 'vitest'
import clientConfig from '../tsdown.client.ts'

const PLUGIN_ROOT = fileURLToPath(new URL('..', import.meta.url))
const REPOSITORY_ROOT = dirname(PLUGIN_ROOT)
const CONFIG_PATH = resolve(PLUGIN_ROOT, 'tsdown.client.ts')
const CLIENT_PATH = resolve(PLUGIN_ROOT, 'lib/client.js')
const SOURCEMAP_PATH = resolve(PLUGIN_ROOT, 'lib/client.js.map')
const CLIENT_SOURCE_PATH = resolve(PLUGIN_ROOT, 'src/client/index.ts')
const STYLE_MANIFEST_KEY = '@shendeguize/dsh-agent-sidecar/style-manifest'
const STYLE_OWNER_KEY = '@shendeguize/dsh-agent-sidecar/style-owner'
const STYLE_GENERATION_KEY = '@shendeguize/dsh-agent-sidecar/style-generation'

const EXPECTED_CSS_MODULES = [
  'src/client/theme/agsc.module.css',
  'src/client/board/board.module.css',
  'src/client/board/project-view.module.css',
  'src/client/settings-card.module.css',
  'src/client/detail/detail.module.css',
  'src/client/dsh-tools/dsh-tools.module.css',
  'src/client/analysis/analysis.module.css',
  'src/client/inject/inject.module.css',
  'src/client/detail-view.module.css',
  'src/client/navigation/center-overlay.module.css',
  'src/client/navigation/sidebar-entry.module.css',
  'src/client/sidebar/sidebar-tab.module.css',
] as const

const USER_ABSOLUTE_PATHS = [
  /\/Users\/[^/"'\\\s]+[\\/]/,
  /\/home\/[^/"'\\\s]+[\\/]/,
  /[A-Z]:[\\/]+Users[\\/]+[^/"'\\\s]+[\\/]+/i,
] as const

type ResolvePlugin = {
  name?: string
  resolveId?: (source: string, importer?: string) => unknown
  load?: (this: { addWatchFile(path: string): void }, id: string) => unknown
}

type KeepStylesAlive = (
  documentRef: Document | undefined,
  globals: Record<PropertyKey, unknown>,
) => () => void

function loadKeepStylesAlive(pluginId: string): KeepStylesAlive {
  const source = readFileSync(CLIENT_SOURCE_PATH, 'utf8')
  const start = source.indexOf('const STYLE_OWNER = Symbol.for(')
  const end = source.indexOf('// Plugin entry.', start)
  if (start === -1 || end === -1) throw new Error('style lifecycle source section not found')
  const helperSource = source.slice(start, end).replace(
    'export function keepStylesAlive',
    'function keepStylesAlive',
  )
  const executable = transpileModule(`${helperSource}\nreturn keepStylesAlive;`, {
    compilerOptions: {
      module: ModuleKind.None,
      target: ScriptTarget.ES2022,
    },
  }).outputText
  return Function('PLUGIN_ID', executable)(pluginId) as KeepStylesAlive
}

class FakeStyle {
  dataset: Record<string, string | undefined>
  textContent: string | null

  constructor(
    private readonly document: FakeStyleDocument,
    dataset: Record<string, string | undefined> = {},
    textContent: string | null = null,
  ) {
    this.dataset = { ...dataset }
    this.textContent = textContent
  }

  remove(): void {
    this.document.remove(this)
  }
}

class FakeStyleDocument {
  readonly styles: FakeStyle[] = []
  readonly queries: string[] = []
  readonly head = {
    appendChild: (tag: FakeStyle): FakeStyle => {
      if (!this.styles.includes(tag)) this.styles.push(tag)
      return tag
    },
  }

  createElement(name: string): FakeStyle {
    if (name !== 'style') throw new Error(`unexpected element: ${name}`)
    return new FakeStyle(this)
  }

  querySelector(selector: string): FakeStyle | null {
    this.queries.push(selector)
    return this.styles.find(tag => this.matches(tag, selector)) ?? null
  }

  querySelectorAll(selector: string): FakeStyle[] {
    this.queries.push(selector)
    return this.styles.filter(tag => this.matches(tag, selector))
  }

  append(tag: FakeStyle): void {
    this.head.appendChild(tag)
  }

  remove(tag: FakeStyle): void {
    const index = this.styles.indexOf(tag)
    if (index !== -1) this.styles.splice(index, 1)
  }

  private matches(tag: FakeStyle, selector: string): boolean {
    if (!selector.startsWith('style')) return false
    for (const match of selector.matchAll(/\[data-(plugin(?:-css)?)=("(?:\\.|[^"])*)"\]/g)) {
      const key = match[1] === 'plugin-css' ? 'pluginCss' : 'plugin'
      const expected = JSON.parse(`${match[2]}"`) as string
      if (tag.dataset[key] !== expected) return false
    }
    return true
  }
}

describe('client CSS build identity', () => {
  it('resolves CSS modules to project-relative POSIX virtual ids', async () => {
    const config = clientConfig as { plugins?: ResolvePlugin[] }
    const cssPlugin = config.plugins?.find(plugin => plugin.name === 'dsh-css-modules-inline')
    expect(cssPlugin?.resolveId).toBeTypeOf('function')

    const importer = resolve(PLUGIN_ROOT, 'src/client/board/Board.tsx')
    const board = await cssPlugin?.resolveId?.('./board.module.css', importer)
    expect(board).toBe('\0dsh-css:src/client/board/board.module.css.mjs')

    const first = await cssPlugin?.resolveId?.(
      resolve(PLUGIN_ROOT, 'src/client/first/shared.module.css'),
    )
    const second = await cssPlugin?.resolveId?.(
      resolve(PLUGIN_ROOT, 'src/client/second/shared.module.css'),
    )
    expect(first).toBe('\0dsh-css:src/client/first/shared.module.css.mjs')
    expect(second).toBe('\0dsh-css:src/client/second/shared.module.css.mjs')
    expect(first).not.toBe(second)
  })

  it('updates only its compound-selected style tag during new materialization', async () => {
    const config = clientConfig as { plugins?: ResolvePlugin[] }
    const cssPlugin = config.plugins?.find(plugin => plugin.name === 'dsh-css-modules-inline')
    const importer = resolve(PLUGIN_ROOT, 'src/client/board/Board.tsx')
    const virtualId = await cssPlugin?.resolveId?.('./board.module.css', importer)
    expect(virtualId).toBeTypeOf('string')
    const generated = await cssPlugin?.load?.call(
      { addWatchFile: () => {} },
      virtualId as string,
    )
    expect(generated).toBeTypeOf('string')

    const packageJson = JSON.parse(
      readFileSync(resolve(PLUGIN_ROOT, 'package.json'), 'utf8'),
    ) as { name: string }
    const tagId = `${packageJson.name}/src/client/board/board.module.css`
    const documentFake = new FakeStyleDocument()
    const foreign = new FakeStyle(
      documentFake,
      { plugin: 'foreign-plugin', pluginCss: tagId },
      'foreign css',
    )
    const existing = new FakeStyle(
      documentFake,
      { plugin: packageJson.name, pluginCss: tagId },
      'old css',
    )
    documentFake.append(foreign)
    documentFake.append(existing)
    const manifest = new Map<string, string>()
    const generation = {}
    const globalFake = {
      [Symbol.for(STYLE_MANIFEST_KEY)]: manifest,
      [Symbol.for(STYLE_GENERATION_KEY)]: generation,
    }
    const executable = (generated as string).replace(/export default [^;]+;$/, '')
    Function('document', 'globalThis', executable)(documentFake, globalFake)

    expect(existing.dataset.plugin).toBe(packageJson.name)
    expect(existing.dataset.pluginCss).toBe(tagId)
    expect(existing.textContent).not.toBe('old css')
    expect((existing as unknown as Record<PropertyKey, unknown>)[Symbol.for(STYLE_OWNER_KEY)])
      .toBe(generation)
    expect(manifest).toEqual(new Map([[tagId, existing.textContent]]))
    expect(foreign.dataset).toEqual({ plugin: 'foreign-plugin', pluginCss: tagId })
    expect(foreign.textContent).toBe('foreign css')
    expect(documentFake.styles).toEqual([foreign, existing])
    expect(documentFake.queries[0]).toBe(
      `style[data-plugin=${JSON.stringify(packageJson.name)}]`
      + `[data-plugin-css=${JSON.stringify(tagId)}]`,
    )
  })

  it('keeps new CSS through old disposal, then prunes and releases current tags', async () => {
    const packageJson = JSON.parse(
      readFileSync(resolve(PLUGIN_ROOT, 'package.json'), 'utf8'),
    ) as { name: string }
    const tagId = `${packageJson.name}/src/client/board/board.module.css`
    const oldKeepStylesAlive = loadKeepStylesAlive(packageJson.name)
    const config = clientConfig as { plugins?: ResolvePlugin[] }
    const cssPlugin = config.plugins?.find(plugin => plugin.name === 'dsh-css-modules-inline')
    const importer = resolve(PLUGIN_ROOT, 'src/client/board/Board.tsx')
    const virtualId = await cssPlugin?.resolveId?.('./board.module.css', importer)
    const generated = await cssPlugin?.load?.call(
      { addWatchFile: () => {} },
      virtualId as string,
    )
    const executable = (generated as string).replace(/export default [^;]+;$/, '')

    const documentFake = new FakeStyleDocument()
    const foreign = new FakeStyle(
      documentFake,
      { plugin: 'foreign-plugin', pluginCss: tagId },
      'foreign css',
    )
    const current = new FakeStyle(
      documentFake,
      { plugin: packageJson.name, pluginCss: tagId },
      'old css',
    )
    documentFake.append(foreign)
    documentFake.append(current)

    const oldGeneration = {}
    const globals: Record<PropertyKey, unknown> = {
      [Symbol.for(STYLE_GENERATION_KEY)]: oldGeneration,
      [Symbol.for(STYLE_MANIFEST_KEY)]: new Map([[tagId, 'old css']]),
    }
    const oldDispose = oldKeepStylesAlive(
      documentFake as unknown as Document,
      globals,
    )

    const newGeneration = {}
    globals[Symbol.for(STYLE_GENERATION_KEY)] = newGeneration
    globals[Symbol.for(STYLE_MANIFEST_KEY)] = new Map<string, string>()
    Function('document', 'globalThis', executable)(documentFake, globals)
    const newCss = (globals[Symbol.for(STYLE_MANIFEST_KEY)] as Map<string, string>).get(tagId)

    expect(newCss).toBeTypeOf('string')
    expect(newCss).not.toBe('old css')
    expect(current.textContent).toBe(newCss)
    expect((current as unknown as Record<PropertyKey, unknown>)[Symbol.for(STYLE_OWNER_KEY)])
      .toBe(newGeneration)
    expect(foreign.textContent).toBe('foreign css')

    oldDispose()
    expect(documentFake.styles).toContain(current)
    expect(current.textContent).toBe(newCss)

    const stale = new FakeStyle(
      documentFake,
      { plugin: packageJson.name, pluginCss: `${packageJson.name}/stale.module.css` },
      'stale css',
    )
    documentFake.append(stale)
    // A new materialized bundle has a fresh module cache.
    const newKeepStylesAlive = loadKeepStylesAlive(packageJson.name)
    const newDispose = newKeepStylesAlive(
      documentFake as unknown as Document,
      globals,
    )

    expect(documentFake.styles).toContain(current)
    expect(documentFake.styles).not.toContain(stale)
    expect(documentFake.styles).toContain(foreign)
    expect(current.textContent).toBe(newCss)
    expect(foreign.textContent).toBe('foreign css')

    newDispose()
    expect(documentFake.styles).not.toContain(current)
    expect(documentFake.styles).toEqual([foreign])
    expect(foreign.dataset).toEqual({ plugin: 'foreign-plugin', pluginCss: tagId })
    expect(foreign.textContent).toBe('foreign css')
  })

  it('limits the manifest cache fallback to the same materialization', () => {
    const packageJson = JSON.parse(
      readFileSync(resolve(PLUGIN_ROOT, 'package.json'), 'utf8'),
    ) as { name: string }
    const tagId = `${packageJson.name}/cached.module.css`
    const keepStylesAlive = loadKeepStylesAlive(packageJson.name)
    const documentFake = new FakeStyleDocument()
    const generation = {}
    const globals: Record<PropertyKey, unknown> = {
      [Symbol.for(STYLE_GENERATION_KEY)]: generation,
      [Symbol.for(STYLE_MANIFEST_KEY)]: new Map([[tagId, 'cached css']]),
    }

    const firstDispose = keepStylesAlive(documentFake as unknown as Document, globals)
    expect(documentFake.styles[0]?.textContent).toBe('cached css')
    firstDispose()
    expect(documentFake.styles).toHaveLength(0)

    delete globals[Symbol.for(STYLE_MANIFEST_KEY)]
    const secondDispose = keepStylesAlive(documentFake as unknown as Document, globals)
    expect(documentFake.styles[0]?.textContent).toBe('cached css')
    secondDispose()

    globals[Symbol.for(STYLE_GENERATION_KEY)] = {}
    globals[Symbol.for(STYLE_MANIFEST_KEY)] = new Map<string, string>()
    const currentDispose = keepStylesAlive(documentFake as unknown as Document, globals)
    expect(documentFake.styles).toHaveLength(0)
    currentDispose()
  })

  it('keeps config and generated artifacts free of user absolute paths', () => {
    const artifacts = [
      [CONFIG_PATH, readFileSync(CONFIG_PATH, 'utf8')],
      [CLIENT_PATH, readFileSync(CLIENT_PATH, 'utf8')],
      [SOURCEMAP_PATH, readFileSync(SOURCEMAP_PATH, 'utf8')],
    ] as const

    for (const [path, contents] of artifacts) {
      expect(contents, path).not.toContain(REPOSITORY_ROOT)
      expect(contents, path).not.toContain(REPOSITORY_ROOT.replaceAll('/', '\\'))
      expect(contents, path).not.toMatch(/file:\/\//i)
      for (const pattern of USER_ABSOLUTE_PATHS) {
        expect(contents, path).not.toMatch(pattern)
      }
    }

    const sourcemap = JSON.parse(artifacts[2][1]) as { sources?: string[] }
    expect(sourcemap.sources?.length).toBeGreaterThan(0)
    for (const source of sourcemap.sources ?? []) {
      expect(source).not.toMatch(/^(?:\/|[A-Z]:[\\/]|file:)/i)
    }
  })

  it('emits every CSS region and injection marker with relative paths', () => {
    const client = readFileSync(CLIENT_PATH, 'utf8')
    const packageJson = JSON.parse(
      readFileSync(resolve(PLUGIN_ROOT, 'package.json'), 'utf8'),
    ) as { name: string }
    const regions = client.match(/\/\/#region \\0dsh-css:[^\r\n]+/g) ?? []

    expect(regions).toHaveLength(EXPECTED_CSS_MODULES.length)
    for (const cssPath of EXPECTED_CSS_MODULES) {
      expect(client).toContain(`//#region \\0dsh-css:${cssPath}.mjs`)
      expect(client).toContain(`${packageJson.name}/${cssPath}`)
    }
  })

  it('resets one current-bundle CSS Map before registering every CSS module', () => {
    const client = readFileSync(CLIENT_PATH, 'utf8')
    const resets = [...client.matchAll(
      /globalThis\[Symbol\.for\("@shendeguize\/dsh-agent-sidecar\/style-manifest"\)\] = (?:\/\* @__PURE__ \*\/ )?new Map\(\);/g,
    )]
    const generations = [...client.matchAll(
      /globalThis\[Symbol\.for\("@shendeguize\/dsh-agent-sidecar\/style-generation"\)\] = \{\};/g,
    )]
    const resetAt = resets[0]?.index ?? -1
    const firstCssAt = client.indexOf('//#region \\0dsh-css:')

    expect(resetAt).toBeGreaterThan(-1)
    expect(resetAt).toBeLessThan(firstCssAt)
    expect(resets).toHaveLength(1)
    expect(generations).toHaveLength(1)
    expect(generations[0]?.index).toBeLessThan(resetAt)

    for (const cssPath of EXPECTED_CSS_MODULES) {
      const start = client.indexOf(`//#region \\0dsh-css:${cssPath}.mjs`)
      const end = client.indexOf('//#endregion', start)
      const region = client.slice(start, end)
      expect(region, cssPath).toMatch(
        /globalThis\[Symbol\.for\("@shendeguize\/dsh-agent-sidecar\/style-manifest"\)\]\.set\(tagId(?:\$\d+)?, css(?:\$\d+)?\);/,
      )
    }
  })
})
