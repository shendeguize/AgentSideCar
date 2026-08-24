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
 * BRIDGE PATH (for the wiring task): each dictionary is a flat
 * `Record<string, string>` — exactly the shape the ecosystem locale
 * service's untyped overload `ctx.locale.register(ns, locale, dict)`
 * accepts — so when the runtime composition provides `ctx.locale`, the
 * wiring half can feed `dictionaries.zh` / `dictionaries.en` straight into
 * it and hand the slot-injected `t` seat to the components instead of the
 * module-local {@link t}. Template params use the same `{name}` syntax as
 * the ecosystem translate.
 */

import { zh } from './zh.ts'
import { en } from './en.ts'
import type { SidecarLocaleDomain, SidecarLocaleKey } from './zh.ts'

export { zh, en }
export type { SidecarLocaleDomain, SidecarLocaleKey }

/** Shipped locales. */
export type SidecarLocale = 'zh' | 'en'

/** Default locale AND the final dictionary consulted before echoing the key. */
export const BASE_LOCALE: SidecarLocale = 'zh'

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
