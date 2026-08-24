/**
 * Locale table contract (T2.3, extended by T5.10b M3 unification): zh/en
 * key-set parity, the t() fallback chain (active locale → zh → the key
 * itself), the `{name}` template semantics, the module-level active-locale
 * switch, and zero-drift parity between the main table and the
 * component-local M3 string tables it references.
 */

import { afterEach, describe, expect, it } from 'vitest'
import {
  BASE_LOCALE,
  createTranslator,
  dictionaries,
  en,
  getLocale,
  setLocale,
  subscribeLocale,
  t,
  zh,
} from '../src/client/locales/index.ts'
import type { SidecarLocaleKey } from '../src/client/locales/index.ts'
import { commandEn, commandZh } from '../src/client/locales/command.ts'
import { DETAIL_STRINGS } from '../src/client/detail/strings.ts'
import { DSH_TOOLS_STRINGS } from '../src/client/dsh-tools/strings.ts'
import { PROJECT_VIEW_STRINGS } from '../src/client/board/project-view-logic.ts'

afterEach(() => { setLocale(BASE_LOCALE) })

/** Flatten a nested string table into `prefix.path.leaf` entries. */
function flatten(prefix: string, table: object): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(table)) {
    if (typeof value === 'string') out[`${prefix}.${key}`] = value
    else Object.assign(out, flatten(`${prefix}.${key}`, value as object))
  }
  return out
}

/** The `domain.*` slice of a flat dictionary. */
function slice(dict: Record<string, string>, domain: string): Record<string, string> {
  return Object.fromEntries(
    Object.entries(dict).filter(([key]) => key.startsWith(`${domain}.`)))
}

describe('dictionary shape', () => {
  it('zh and en carry identical key sets', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort())
  })

  it('every entry is a non-empty string in both locales', () => {
    for (const dict of [zh, en]) {
      for (const [key, value] of Object.entries(dict)) {
        expect(value, `empty copy for ${key}`).toBeTypeOf('string')
        expect(value.length, `empty copy for ${key}`).toBeGreaterThan(0)
      }
    }
  })

  it('every key sits in a declared domain', () => {
    for (const key of Object.keys(zh)) {
      expect(key).toMatch(
        /^(settings|board|inject|detail|dshtools|project|analysis|command|sidebar)\.[^.].*$/)
    }
  })

  it('every declared domain is populated', () => {
    for (const domain of
      ['settings', 'board', 'inject', 'detail', 'dshtools', 'project', 'analysis', 'command',
        'sidebar']) {
      expect(Object.keys(slice(zh, domain)).length, `empty domain ${domain}`).toBeGreaterThan(0)
    }
  })

  it('exports the shipped dictionaries under their locale ids', () => {
    expect(dictionaries.zh).toBe(zh)
    expect(dictionaries.en).toBe(en)
  })
})

describe('M3 unification: main table ↔ module tables (zero copy drift)', () => {
  it('detail.* covers detail/strings.ts verbatim', () => {
    const moduleTable = flatten('detail', DETAIL_STRINGS)
    for (const [key, copy] of Object.entries(moduleTable)) {
      expect(zh[key as SidecarLocaleKey], `drift at ${key}`).toBe(copy)
    }
  })

  it('dshtools.* covers dsh-tools/strings.ts verbatim', () => {
    const moduleTable = flatten('dshtools', DSH_TOOLS_STRINGS)
    expect(slice(zh, 'dshtools')).toEqual(moduleTable)
  })

  it('project.* covers PROJECT_VIEW_STRINGS verbatim', () => {
    const moduleTable = flatten('project', PROJECT_VIEW_STRINGS)
    expect(slice(zh, 'project')).toEqual(moduleTable)
  })

  it('the command segment is exactly the command.* slice of the main table', () => {
    expect({ ...commandZh }).toEqual(slice(zh, 'command'))
    expect({ ...commandEn }).toEqual(slice(en, 'command'))
  })

  it('t() serves M3 domain keys in both locales', () => {
    expect(t('dshtools.search.filterOnlyNotice'))
      .toBe(DSH_TOOLS_STRINGS.search.filterOnlyNotice)
    expect(t('project.title')).toBe(PROJECT_VIEW_STRINGS.title)
    expect(t('analysis.disclaimerFallback')).toBe(zh['analysis.disclaimerFallback'])
    setLocale('en')
    expect(t('detail.header.close')).toBe(en['detail.header.close'])
    expect(t('analysis.start')).toBe(en['analysis.start'])
  })
})

describe('t() on the shipped table', () => {
  it('defaults to zh', () => {
    expect(getLocale()).toBe('zh')
    expect(t('settings.save')).toBe(zh['settings.save'])
  })

  it('switches to en via setLocale', () => {
    setLocale('en')
    expect(getLocale()).toBe('en')
    expect(t('settings.save')).toBe(en['settings.save'])
  })

  it('echoes an unknown key verbatim (final fallback)', () => {
    const missing = 'settings.doesNotExist' as SidecarLocaleKey
    expect(t(missing)).toBe('settings.doesNotExist')
    setLocale('en')
    expect(t(missing)).toBe('settings.doesNotExist')
  })

  it('substitutes {name} params and leaves unknown placeholders verbatim', () => {
    expect(t('settings.daemonPidVersion', { pid: 42, version: '1.2.3' }))
      .toBe('pid 42 · v1.2.3')
    expect(t('settings.daemonPidVersion', { pid: 42 }))
      .toBe('pid 42 · v{version}')
  })

  it('notifies locale subscribers once per switch and honors the disposer', () => {
    let calls = 0
    const dispose = subscribeLocale(() => { calls += 1 })
    setLocale('en')
    expect(calls).toBe(1)
    setLocale('en') // unchanged: no notification
    expect(calls).toBe(1)
    dispose()
    setLocale('zh')
    expect(calls).toBe(1)
  })
})

describe('createTranslator fallback chain', () => {
  it('missing en key falls back to zh, then to the key itself', () => {
    const translate = createTranslator({
      zh: { 'settings.onlyZh': '仅中文', 'settings.both': '两边都有' },
      en: { 'settings.both': 'present in both' },
    })
    // en hit
    expect(translate('en', 'settings.both')).toBe('present in both')
    // en miss → zh
    expect(translate('en', 'settings.onlyZh')).toBe('仅中文')
    // both miss → key itself
    expect(translate('en', 'settings.nowhere')).toBe('settings.nowhere')
    expect(translate('zh', 'settings.nowhere')).toBe('settings.nowhere')
  })

  it('survives an entirely absent locale dictionary', () => {
    const translate = createTranslator({ zh: { 'settings.k': '值' } })
    expect(translate('en', 'settings.k')).toBe('值')
    expect(translate('en', 'settings.missing')).toBe('settings.missing')
  })

  it('interpolates through the fallback layer too', () => {
    const translate = createTranslator({ zh: { 'settings.greet': '你好 {who}' } })
    expect(translate('en', 'settings.greet', { who: '世界' })).toBe('你好 世界')
  })
})
