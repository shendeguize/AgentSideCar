/**
 * Host-half build: the cordis plugin loaded by the dsh host process from
 * `exports["."]` (lib/index.js). The dsh peer surfaces stay external — they
 * resolve at runtime from the dsh profile tree, never from this package's
 * own install; schemastery is a real dependency and resolves from this
 * package's node_modules.
 */
import { defineConfig } from 'tsdown'

export default defineConfig({
  name: '@shendeguize/dsh-agent-sidecar',
  entry: ['src/index.ts'],
  outDir: 'lib',
  format: ['esm'],
  platform: 'node',
  target: 'es2022',
  fixedExtension: false,
  dts: true,
  sourcemap: false,
  clean: false,
  external: [/^@deepseek-ai\//],
})
