/**
 * `command.*` locale segment for the `/sidecar` slash command (T4.6).
 *
 * T5.10b unification: the `command.*` copy now LIVES in the main
 * dictionaries (./zh.ts + ./en.ts, domain `command`) and this module is a
 * thin derived view kept for its consumers ({@link tCommand} for
 * commands.ts, {@link commandZh}/{@link commandEn} for tests and for the
 * `ctx.locale.register(ns, locale, dict)` bridge). Translation goes
 * through the shared engine and the shared active-locale switch, so
 * `setLocale` drives every surface consistently and the main-table parity
 * test covers these keys too.
 *
 * @module
 */

import { t, zh, en } from './index.ts'
import type { KeysOfDomain } from './index.ts'

/** Key union of the command locale segment (main zh table is the source). */
export type CommandLocaleKey = KeysOfDomain<'command'>

function commandSlice(dict: Readonly<Record<string, string>>): Record<CommandLocaleKey, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(dict)) {
    if (key.startsWith('command.')) out[key] = value
  }
  return out as Record<CommandLocaleKey, string>
}

/** The `command.*` slice of the main zh dictionary. */
export const commandZh: Readonly<Record<CommandLocaleKey, string>> = commandSlice(zh)

/** The `command.*` slice of the main en dictionary. */
export const commandEn: Readonly<Record<CommandLocaleKey, string>> = commandSlice(en)

/** Derived dictionaries of this segment, keyed by locale id. */
export const commandDictionaries = { zh: commandZh, en: commandEn } as const

/**
 * Translate a command-segment key in the shared active locale (delegates
 * to the main-table {@link t}; lookup chain: active locale → zh → key).
 * @param key - a key of the command segment.
 * @param params - optional `{name}` template params.
 * @returns the translated string.
 */
export function tCommand(key: CommandLocaleKey, params?: Record<string, unknown>): string {
  return t(key, params)
}
