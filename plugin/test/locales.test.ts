/**
 * Locale table contract (T2.3): zh/en key-set parity, the t() fallback
 * chain (active locale → zh → the key itself), the `{name}` template
 * semantics, and the module-level active-locale switch.
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

afterEach(() => { setLocale(BASE_LOCALE) })

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

  it('every key sits in a declared domain (settings live, board/inject reserved)', () => {
    for (const key of Object.keys(zh)) {
      expect(key).toMatch(/^(settings|board|inject)\.[^.].*$/)
    }
  })

  it('the settings card domain is populated', () => {
    const settingsKeys = Object.keys(zh).filter(key => key.startsWith('settings.'))
    expect(settingsKeys.length).toBeGreaterThan(0)
  })

  it('exports the shipped dictionaries under their locale ids', () => {
    expect(dictionaries.zh).toBe(zh)
    expect(dictionaries.en).toBe(en)
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
