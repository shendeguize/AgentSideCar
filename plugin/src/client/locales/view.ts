import { t, type SidecarLocaleKey } from './index.ts'

/**
 * A nested map from view-facing names to keys in the shipped locale table.
 * Leaves are deliberately parameterless; callers format translated templates.
 */
export type LocaleViewDescriptor = Readonly<{
  [name: string]: SidecarLocaleKey | LocaleViewDescriptor
}>

/** Preserve a descriptor's readonly shape while translating every leaf. */
export type LocaleView<D extends LocaleViewDescriptor> = {
  readonly [K in keyof D]:
    D[K] extends SidecarLocaleKey ? string
      : D[K] extends LocaleViewDescriptor ? LocaleView<D[K]>
        : never
}

function buildNode(descriptor: LocaleViewDescriptor): Record<string, unknown> {
  const view: Record<string, unknown> = {}
  for (const [name, value] of Object.entries(descriptor)) {
    const property: PropertyDescriptor = typeof value === 'string'
      ? { enumerable: true, get: () => t(value) }
      : { enumerable: true, value: buildNode(value) }
    Object.defineProperty(view, name, property)
  }
  return view
}

/**
 * Build one stable, enumerable locale facade. Leaf getters call {@link t} at
 * read time, so the same facade follows subsequent active-locale changes.
 */
export function createLocaleView<const D extends LocaleViewDescriptor>(
  descriptor: D,
): LocaleView<D> {
  return buildNode(descriptor) as LocaleView<D>
}
