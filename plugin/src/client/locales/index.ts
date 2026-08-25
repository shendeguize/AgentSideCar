/**
 * Module-owned lightweight locale table for the agent-sidecar client half.
 *
 * WHY MODULE-OWNED (source-audit conclusion, T2.3): the installed SDK
 * surface (`@deepseek-ai/dsh-client-runtime` 0.1.1-rc.2 +
 * `@deepseek-ai/dsh-client-ui-slots`) ships only the locale TYPE currency
 * (`LocaleNamespaceMap`, `Translate`, the `locale:` register option and the
 * `t` standard seat) plus the boot-once `slots.installLocale(face)` hook;
 * the `ctx.locale` service itself lives in a separate plugin
 * (`@deepseek-ai/dsh-client-locale`, harness `packages/client/locale`) that
 * is NOT part of this package's dependency set. This table therefore keeps
 * the copy self-contained: default zh, switchable en, with a `t(key)`
 * helper whose lookup chain is active locale → zh → the key itself (fail
 * visible, never blank — same posture as the ecosystem LocaleRuntime,
 * which falls back to en; ours falls back to zh per this plugin's spec).
 *
 * The optional Host bridge in ./host.ts registers these flat dictionaries
 * through the ecosystem locale service's untyped
 * `register(ns, locale, dict)` overload and mirrors `locale/change` into
 * this module's active locale. The local table remains the fallback whenever
 * that service is absent. Template params use the same `{name}` syntax as
 * the ecosystem translate.
 */

import { zh } from './zh.ts'
import { en } from './en.ts'
import {
  bridgeHostLocale as bridgeHostLocalePort,
  type HostLocalePort as HostLocalePortFace,
  type HostLocaleService as HostLocaleServiceFace,
  type HostMappedLocale as HostMappedLocaleId,
} from './host.ts'
import type { SidecarLocaleDomain, SidecarLocaleKey } from './zh.ts'

export { zh, en }
export type { SidecarLocaleDomain, SidecarLocaleKey }
export { bridgeHostLocale, mapHostLocale } from './host.ts'
export type {
  HostLocaleBridgeOptions,
  HostLocaleDictionary,
  HostLocaleDisposer,
  HostLocalePort,
  HostLocaleService,
  HostLocaleSnapshot,
  HostLocaleValue,
  HostMappedLocale,
} from './host.ts'

/** Shipped locales. */
export type SidecarLocale = HostMappedLocaleId

/** Default locale AND the final dictionary consulted before echoing the key. */
export const BASE_LOCALE: SidecarLocale = 'zh'

/** Host namespace owned by Agent Sidecar dictionaries. */
export const SIDECAR_LOCALE_NAMESPACE = 'agent-sidecar'

/** One flat dictionary (the `ctx.locale.register(ns, locale, dict)` currency). */
export type SidecarDict = Readonly<Record<string, string>>

/** Complete shipped dictionaries keyed by locale id. */
export const dictionaries: Readonly<Record<SidecarLocale, SidecarDict>> = { zh, en }

/** Keys of one domain (`settings.` / `inject.` are live; `board.` stays reserved). */
export type KeysOfDomain<D extends SidecarLocaleDomain> = Extract<
  SidecarLocaleKey,
  `${D}.${string}`
>

/** The settings card's own key union. */
export type SettingsLocaleKey = KeysOfDomain<'settings'>

/**
 * Substitute `{name}` template params; unknown placeholders stay verbatim
 * (same semantics as the ecosystem LocaleRuntime.translate).
 */
function interpolate(template: string, params?: Record<string, unknown>): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in params ? String(params[name]) : match)
}

/**
 * Build a translate engine over an arbitrary (possibly partial) dictionary
 * set. Lookup chain per key: requested locale → {@link BASE_LOCALE} (zh) →
 * the key itself. Exposed for tests and for compositions that need a
 * non-shipped dictionary set; the shipped {@link t} is this engine bound to
 * {@link dictionaries} and the module's active locale.
 * @param dicts - dictionaries keyed by locale id (missing locales allowed).
 * @returns pure translate function addressed by explicit locale.
 */
export function createTranslator(
  dicts: Partial<Record<SidecarLocale, SidecarDict>>,
): (locale: SidecarLocale, key: string, params?: Record<string, unknown>) => string {
  return (locale, key, params) => {
    const template = dicts[locale]?.[key] ?? dicts[BASE_LOCALE]?.[key] ?? key
    return interpolate(template, params)
  }
}

const shippedTranslate = createTranslator(dictionaries)

let activeLocale: SidecarLocale = BASE_LOCALE
const localeListeners = new Set<() => void>()

/** @returns the active locale id. */
export function getLocale(): SidecarLocale {
  return activeLocale
}

/**
 * Switch the active locale and notify subscribers (no-op when unchanged).
 * @param locale - a shipped locale id.
 */
export function setLocale(locale: SidecarLocale): void {
  if (activeLocale === locale) return
  activeLocale = locale
  for (const fn of [...localeListeners]) fn()
}

/**
 * Observe active-locale switches (React consumers pair this with
 * {@link getLocale} as a uSES source).
 * @param fn - change callback.
 * @returns unsubscribe.
 */
export function subscribeLocale(fn: () => void): () => void {
  localeListeners.add(fn)
  return () => { localeListeners.delete(fn) }
}

interface HostLocaleOwner {
  host: HostLocalePortFace
  service: HostLocaleServiceFace
  onLocale: (locale: SidecarLocale) => void
}

interface HostLocaleLease {
  owners: Set<HostLocaleOwner>
  detachDictionaries: () => void
}

interface HostLocaleLeaseRegistry {
  get(service: HostLocaleServiceFace): HostLocaleLease | undefined
  set(service: HostLocaleServiceFace, lease: HostLocaleLease): unknown
  delete(service: HostLocaleServiceFace): boolean
}

interface HostLocaleOwnerSet {
  readonly size: number
  add(owner: HostLocaleOwner): unknown
  delete(owner: HostLocaleOwner): boolean
  [Symbol.iterator](): IterableIterator<HostLocaleOwner>
}

interface HostLocaleArbiter {
  leases: HostLocaleLeaseRegistry
  owners: HostLocaleOwnerSet
  activeOwner: HostLocaleOwner | null
  detachEvents: () => void
}

const HOST_LOCALE_LEASES_SYMBOL = Symbol.for(
  '@shendeguize/dsh-agent-sidecar/host-locale-leases',
)
const globalSymbols = globalThis as typeof globalThis & { [key: symbol]: unknown }

function hasMethod<K extends PropertyKey>(
  value: unknown,
  key: K,
): value is Record<K, (...args: never[]) => unknown> {
  return typeof value === 'object'
    && value !== null
    && typeof (value as Record<K, unknown>)[key] === 'function'
}

function isLeaseRegistry(value: unknown): value is HostLocaleLeaseRegistry {
  return hasMethod(value, 'get') && hasMethod(value, 'set') && hasMethod(value, 'delete')
}

function isOwnerSet(value: unknown): value is HostLocaleOwnerSet {
  return hasMethod(value, 'add')
    && hasMethod(value, 'delete')
    && hasMethod(value, Symbol.iterator)
    && typeof (value as { size?: unknown }).size === 'number'
}

function isHostLocaleArbiter(value: unknown): value is HostLocaleArbiter {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Partial<HostLocaleArbiter>
  return isLeaseRegistry(candidate.leases)
    && isOwnerSet(candidate.owners)
    && (candidate.activeOwner === null || typeof candidate.activeOwner === 'object')
    && typeof candidate.detachEvents === 'function'
}

const sharedLocaleState = globalSymbols[HOST_LOCALE_LEASES_SYMBOL]
const hostLocaleArbiter: HostLocaleArbiter = isHostLocaleArbiter(sharedLocaleState)
  ? sharedLocaleState
  : {
      // Preserve a pre-arbiter WeakMap-like registry across an HMR upgrade.
      leases: isLeaseRegistry(sharedLocaleState)
        ? sharedLocaleState
        : new WeakMap<HostLocaleServiceFace, HostLocaleLease>(),
      owners: new Set<HostLocaleOwner>(),
      activeOwner: null,
      detachEvents: () => {},
    }
globalSymbols[HOST_LOCALE_LEASES_SYMBOL] = hostLocaleArbiter

function bridge(
  host: HostLocalePortFace,
  onLocale: (locale: SidecarLocale) => void,
): () => void {
  return bridgeHostLocalePort(host, {
    namespace: SIDECAR_LOCALE_NAMESPACE,
    dictionaries,
    onLocale,
  })
}

function followOwner(owner: HostLocaleOwner): () => void {
  return bridge({
    locale: { getLocale: () => owner.service.getLocale?.() },
    on: (event, listener) => owner.host.on?.(event, listener),
  }, locale => {
    if (hostLocaleArbiter.activeOwner === owner) owner.onLocale(locale)
  })
}

function latestOwner(owners: HostLocaleOwnerSet): HostLocaleOwner {
  let latest: HostLocaleOwner | undefined
  for (const owner of owners) latest = owner
  return latest as HostLocaleOwner
}

function detachActiveFollower(): void {
  const detach = hostLocaleArbiter.detachEvents
  hostLocaleArbiter.detachEvents = () => {}
  try {
    detach()
  } catch {
    // Cross-bundle optional Host teardown remains best-effort.
  }
}

function activateOwner(owner: HostLocaleOwner): void {
  detachActiveFollower()
  hostLocaleArbiter.activeOwner = owner
  hostLocaleArbiter.detachEvents = followOwner(owner)
}

/**
 * Lease the shipped dictionaries and locale following for one Host service.
 * Dictionary ownership is keyed by service identity. Event ownership is
 * global across services and bundles so only the latest attached owner can
 * drive its module-local locale.
 */
export function attachHostLocale(host: HostLocalePortFace | null | undefined): () => void {
  if (host?.locale === undefined) return () => {}
  const service = host.locale

  const owner: HostLocaleOwner = { host, service, onLocale: setLocale }
  let lease = hostLocaleArbiter.leases.get(service)
  if (lease === undefined) {
    lease = {
      owners: new Set<HostLocaleOwner>(),
      // Registration must never choose this module's active locale.
      detachDictionaries: bridge({ locale: service }, () => {}),
    }
    hostLocaleArbiter.leases.set(service, lease)
  }
  lease.owners.add(owner)
  hostLocaleArbiter.owners.add(owner)
  activateOwner(owner)

  let disposed = false
  return () => {
    if (disposed) return
    disposed = true
    const wasActive = hostLocaleArbiter.activeOwner === owner
    lease.owners.delete(owner)
    hostLocaleArbiter.owners.delete(owner)
    if (wasActive) {
      detachActiveFollower()
      hostLocaleArbiter.activeOwner = null
    }
    if (lease.owners.size === 0) {
      lease.detachDictionaries()
      hostLocaleArbiter.leases.delete(service)
    }
    if (!wasActive) return

    if (hostLocaleArbiter.owners.size > 0) {
      activateOwner(latestOwner(hostLocaleArbiter.owners))
      return
    }
    owner.onLocale(BASE_LOCALE)
  }
}

/**
 * Translate a typed key in the active locale. Missing entries fall back to
 * zh, then to the key itself (see module doc for why the chain ends visible).
 * @param key - a key of the shipped table.
 * @param params - optional `{name}` template params.
 * @returns the translated string.
 */
export function t(key: SidecarLocaleKey, params?: Record<string, unknown>): string {
  return shippedTranslate(activeLocale, key, params)
}
