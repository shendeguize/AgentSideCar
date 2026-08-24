/**
 * Browser client bundle for @shendeguize/dsh-agent-sidecar, replicating the
 * DeepSeek Harness lazy-CJS `clientBundle` preset (the official preset is
 * unpublished; blueprint: dsh-web-ui `shared/tsdown.client.ts`, cross-checked
 * against dsh-agent-teams' standalone replica):
 *
 * - CJS closure-factory artifact: `window.__ModuleLoader__.load({ id,
 *   factory: (require) => ... })`; executing the script only REGISTERS the
 *   factory — module body side effects run at materialization. Externals
 *   resolve through the loader module table (platform seed entries + the
 *   parser-preloaded runtime row).
 * - CSS Modules are compiled by lightningcss into hashed class maps; the css
 *   text auto-injects a `<style data-plugin>` tag at factory execution.
 * - Every other @deepseek-ai value import is a build error (purity gate):
 *   a cross-plugin value import either inlines a duplicate runtime instance
 *   or requires a specifier the frozen module table cannot answer.
 */

import { existsSync, readFileSync } from 'node:fs'
import { basename, dirname, resolve as resolvePath, sep } from 'node:path'
import { transform } from 'lightningcss'
import { defineConfig, type UserConfig } from 'tsdown'

/** Platform seed entries the browser module table answers (external). */
const PLATFORM_MODULES = [
  'react', 'react/jsx-runtime', 'react-dom', 'react-dom/client', '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-primitives',
]

/** Dynamic rows whose factories the rc.2 shell preloads before plugin boot. */
const PRELOADED_CLIENT_EXTERNALS = ['@deepseek-ai/dsh-client-runtime/client']

/** Externals resolved from the loader module table. */
const CLIENT_EXTERNALS: readonly string[] = [...PLATFORM_MODULES, ...PRELOADED_CLIENT_EXTERNALS]

/** Wire/type layers a client bundle may inline (no shared runtime identity). */
const INLINE_SAFE = /^@deepseek-ai\/dsh-(host-apiproxy|session|llm|tools|brand)(\/|$)/

/** Vendored framework libraries (no cross-plugin runtime identity). */
const VENDORED_LIBRARY = /^@deepseek-ai\/(cosmokit|schemastery)(\/|$)/

/** Generated descriptor/codec contributions (no shared runtime identity). */
const GENERATED_REMOTE = /^@deepseek-ai\/dsh-[a-z0-9]+(?:-[a-z0-9]+)*\/remote$/

/** Virtual-id wrapper keeping module CSS away from tsdown's own css pipeline. */
const CSS_VIRTUAL_PREFIX = '\0dsh-css:'
const CSS_VIRTUAL_SUFFIX = '.mjs'

/**
 * Module id this bundle registers under via `__ModuleLoader__.load`. The host
 * client-modules registry keys graph rows by the plugin's package name, so
 * this must BE the package name — read from package.json rather than restated,
 * so a rename cannot leave the client half registering a stale id.
 */
const PLUGIN_ID: string = (JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
) as { name: string }).name

const config: UserConfig = {
  name: `${PLUGIN_ID}/client`,
  entry: { client: 'src/client/index.ts' },
  // Browser bundle lands next to the node half (single lib/ artifact dir; the
  // entryFileNames pin keeps it exactly lib/client.js). clean must stay off —
  // a default clean would wipe the node-half output.
  outDir: 'lib',
  format: 'cjs',
  platform: 'browser',
  // dts would wrap the banner/footer into .d.cts and break parsing.
  dts: false,
  // Plugin code is fetched outside Vite's module graph, so its own bundle
  // carries the TS mapping consumed by browser profiling tools.
  sourcemap: true,
  clean: false,
  external: [...CLIENT_EXTERNALS],
  // Anything NOT in the loader module table must inline instead: a require()
  // the table cannot answer is a guaranteed runtime throw, so the rule is the
  // table list itself — external wins for table entries, bundle everything else.
  noExternal: (id: string) => (CLIENT_EXTERNALS.includes(id) ? undefined : true),
  // Inlined node-idiom deps read process.env.NODE_ENV / import.meta.env at
  // runtime; a CJS browser bundle cannot carry import.meta, so both keys are
  // substituted at build time (artifacts default to production).
  define: {
    'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV ?? 'production'),
    'import.meta.env.MODE': JSON.stringify(process.env.NODE_ENV ?? 'production'),
    'import.meta.env': JSON.stringify({ MODE: process.env.NODE_ENV ?? 'production' }),
  },
  plugins: [{
    // Bundle purity gate (build-time mirror of the module-edge rules).
    name: 'dsh-client-bundle-purity',
    resolveId(source: string) {
      if (!source.startsWith('@deepseek-ai/')) return null
      if (CLIENT_EXTERNALS.includes(source)) return null // platform module: external wins
      if (VENDORED_LIBRARY.test(source)) return null // vendored library: inline
      if (INLINE_SAFE.test(source) || GENERATED_REMOTE.test(source)) return null // wire layer: inline is the point
      throw new Error(
        `client bundle purity: "${source}" is not a platform module (CLIENT_EXTERNALS), an inline-safe wire layer, or a generated /remote contribution — `
        + 'cross-plugin value imports are forbidden; collaborate through cordis services (type-only imports are erased and never reach this gate)',
      )
    },
  }, {
    name: 'dsh-css-modules-inline',
    resolveId(source: string, importer: string | undefined) {
      if (!source.endsWith('.module.css')) return null
      const abs = importer !== undefined ? sourceAssetPath(source, importer) : source
      return CSS_VIRTUAL_PREFIX + abs + CSS_VIRTUAL_SUFFIX
    },
    async load(virtualId: string) {
      if (!virtualId.startsWith(CSS_VIRTUAL_PREFIX)) return null
      const fileId = virtualId.slice(CSS_VIRTUAL_PREFIX.length, -CSS_VIRTUAL_SUFFIX.length)
      this.addWatchFile(fileId)
      const source = readFileSync(fileId)
      const { code, exports: cssExports } = transform({
        filename: fileId,
        code: source,
        cssModules: { pattern: '[hash]_[local]' },
        minify: true,
      })
      // Sorted so the emitted map is byte-stable across rebuilds.
      const classMap: Record<string, string> = {}
      const sorted = Object.entries(cssExports ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      for (const [local, exp] of sorted) classMap[local] = exp.name
      return [
        `const css = ${JSON.stringify(code.toString())};`,
        `const tagId = ${JSON.stringify(`${PLUGIN_ID}/${basename(fileId)}`)};`,
        'if (typeof document !== \'undefined\' && document.querySelector(\'style[data-plugin-css=\' + JSON.stringify(tagId) + \']\') === null) {',
        '  const tag = document.createElement(\'style\');',
        `  tag.dataset.plugin = ${JSON.stringify(PLUGIN_ID)};`,
        '  tag.dataset.pluginCss = tagId;',
        '  tag.textContent = css;',
        '  document.head.appendChild(tag);',
        '}',
        `export default ${JSON.stringify(classMap)};`,
      ].join('\n')
    },
  }],
  outputOptions: {
    entryFileNames: 'client.js',
    banner: `window.__ModuleLoader__.load({ id: ${JSON.stringify(PLUGIN_ID)}, factory: (require) => {`,
    footer: 'return module.exports; } });',
    intro: 'var module = { exports: {} }; var exports = module.exports;',
  },
}

/** Resolve an emitted JS asset import against its source-tree counterpart. */
function sourceAssetPath(source: string, importer: string): string {
  const emitted = resolvePath(dirname(importer), source)
  if (existsSync(emitted)) return emitted
  const marker = `${sep}lib${sep}`
  const libIndex = emitted.indexOf(marker)
  if (libIndex !== -1) {
    const srcPath = `${emitted.slice(0, libIndex)}${sep}src${sep}${emitted.slice(libIndex + marker.length)}`
    if (existsSync(srcPath)) return srcPath
  }
  return source
}

export default defineConfig(config)
