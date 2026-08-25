/**
 * Capability-based bridge to the optional dsh locale service.
 *
 * API evidence:
 * `.local/reference/deepseek-harness/packages/client/locale/src/client/index.ts`
 * exposes `getLocale().active`, `register(ns, locale, dict)`, and the
 * context event `locale/change`. This module mirrors only those shapes; it
 * deliberately imports no unpublished host runtime value.
 */

/** Locale ids shipped by Agent Sidecar. */
export type HostMappedLocale = 'zh' | 'en'

/** Flat namespace dictionary accepted by the host registry. */
export type HostLocaleDictionary = Readonly<Record<string, string>>

/** Locale-like values accepted from getters and change events. */
export interface HostLocaleSnapshot {
  active?: string
  locale?: string
  preference?: string
  value?: { preference?: string }
}

export type HostLocaleValue = string | HostLocaleSnapshot | null | undefined
export type HostLocaleDisposer = () => void

/** Optional locale service capabilities used by the bridge. */
export interface HostLocaleService {
  getLocale?: () => HostLocaleValue
  register?: (
    namespace: string,
    locale: string,
    dictionary: HostLocaleDictionary,
  ) => void | HostLocaleDisposer
}

/** Optional host-context capabilities used by the bridge. */
export interface HostLocalePort {
  locale?: HostLocaleService
  on?: (
    event: 'locale/change',
    listener: (value?: HostLocaleValue) => void,
  ) => void | HostLocaleDisposer
}

/** Injected policy and data; keeps the adapter independent of locale state. */
export interface HostLocaleBridgeOptions {
  namespace: string
  dictionaries: Readonly<Record<HostMappedLocale, HostLocaleDictionary>>
  onLocale: (locale: HostMappedLocale) => void
}

function localeTag(value: HostLocaleValue): string | undefined {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return undefined
  return value.active
    ?? value.preference
    ?? value.locale
    ?? value.value?.preference
}

/** Map every Chinese locale variant to zh; all other values use en. */
export function mapHostLocale(value: HostLocaleValue): HostMappedLocale {
  return localeTag(value)?.trim().toLowerCase().startsWith('zh') === true ? 'zh' : 'en'
}

/**
 * Register dictionaries, adopt the current Host locale, and follow changes.
 * Every capability is optional and isolated: absent or throwing Host methods
 * leave the module-owned locale fallback intact.
 */
export function bridgeHostLocale(
  host: HostLocalePort | null | undefined,
  options: HostLocaleBridgeOptions,
): HostLocaleDisposer {
  const disposers: HostLocaleDisposer[] = []
  const service = host?.locale

  for (const locale of ['zh', 'en'] as const) {
    try {
      const dispose = service?.register?.(
        options.namespace,
        locale,
        options.dictionaries[locale],
      )
      if (typeof dispose === 'function') disposers.push(dispose)
    } catch {
      // Optional/duplicate registry seats must not block the local fallback.
    }
  }

  const read = (): HostLocaleValue => {
    try {
      return service?.getLocale?.()
    } catch {
      return undefined
    }
  }
  const apply = (value: HostLocaleValue): void => {
    if (localeTag(value) === undefined) return
    try {
      options.onLocale(mapHostLocale(value))
    } catch {
      // A consumer failure must not escape a Host event callback.
    }
  }

  try {
    const dispose = host?.on?.('locale/change', (value) => {
      apply(value === undefined ? read() : value)
    })
    if (typeof dispose === 'function') disposers.push(dispose)
  } catch {
    // Event capability is optional.
  }
  apply(read())

  let disposed = false
  return () => {
    if (disposed) return
    disposed = true
    for (const dispose of disposers.reverse()) {
      try {
        dispose()
      } catch {
        // Teardown remains best-effort across independently owned services.
      }
    }
  }
}
