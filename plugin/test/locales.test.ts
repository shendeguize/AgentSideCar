/**
 * Locale table contract (T2.3, extended by the Host locale bridge): zh/en
 * key-set parity, the t() fallback chain (active locale → zh → the key
 * itself), the `{name}` template semantics, the module-level active-locale
 * switch, and capability-based Host registration/change synchronization.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  attachHostLocale,
  BASE_LOCALE,
  bridgeHostLocale,
  createTranslator,
  dictionaries,
  en,
  getLocale,
  mapHostLocale,
  setLocale,
  SIDECAR_LOCALE_NAMESPACE,
  subscribeLocale,
  t,
  zh,
} from '../src/client/locales/index.ts'
import type {
  HostLocalePort,
  HostLocaleService,
  HostLocaleValue,
  SidecarLocaleKey,
} from '../src/client/locales/index.ts'
import { createLocaleView } from '../src/client/locales/view.ts'
import { commandEn, commandZh } from '../src/client/locales/command.ts'
import { formatTemplate } from '../src/client/detail/logic.ts'

afterEach(() => { setLocale(BASE_LOCALE) })

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

describe('main dictionary translations', () => {
  it('the command segment is exactly the command.* slice of the main table', () => {
    expect({ ...commandZh }).toEqual(slice(zh, 'command'))
    expect({ ...commandEn }).toEqual(slice(en, 'command'))
  })

  it('t() serves M3 domain keys in both locales', () => {
    expect(t('dshtools.search.filterOnlyNotice'))
      .toBe('dsh 全文检索不可用,已降级为标题/项目过滤')
    expect(t('project.title')).toBe('项目关联')
    expect(t('analysis.disclaimerFallback')).toBe(zh['analysis.disclaimerFallback'])
    setLocale('en')
    expect(t('detail.header.close')).toBe(en['detail.header.close'])
    expect(t('analysis.start')).toBe(en['analysis.start'])
  })
})

describe('locale view facade', () => {
  it('keeps its nested shape while following zh → en switches', () => {
    setLocale('zh')
    const view = createLocaleView({
      header: { close: 'detail.header.close' },
      project: { title: 'project.title' },
    } as const)
    const header = view.header

    expect(view.header.close).toBe(zh['detail.header.close'])
    expect(view.project.title).toBe(zh['project.title'])
    setLocale('en')
    expect(view.header).toBe(header)
    expect(view.header.close).toBe(en['detail.header.close'])
    expect(view.project.title).toBe(en['project.title'])
  })

  it('keeps every level enumerable without modifying the descriptor', () => {
    const descriptor = Object.freeze({
      header: Object.freeze({ close: 'detail.header.close' as const }),
      title: 'project.title' as const,
    })
    const view = createLocaleView(descriptor)

    expect(Object.keys(view)).toEqual(['header', 'title'])
    expect(Object.keys(view.header)).toEqual(['close'])
    expect(Object.entries(view.header)).toEqual([['close', zh['detail.header.close']]])
    expect(descriptor).toEqual({
      header: { close: 'detail.header.close' },
      title: 'project.title',
    })
    expect(view).not.toBe(descriptor)
    expect(view.header).not.toBe(descriptor.header)
  })

  it('leaves translated template placeholders for callers to format separately', () => {
    const view = createLocaleView({
      daemonPidVersion: 'settings.daemonPidVersion',
    } as const)

    expect(view.daemonPidVersion).toBe('pid {pid} · v{version}')
    expect(formatTemplate(view.daemonPidVersion, { pid: 42, version: '1.2.3' }))
      .toBe('pid 42 · v1.2.3')
  })
})

describe('Host locale bridge', () => {
  it('maps Chinese variants to zh and every other locale to en', () => {
    expect(mapHostLocale('zh')).toBe('zh')
    expect(mapHostLocale('zh-CN')).toBe('zh')
    expect(mapHostLocale({ active: 'ZH-Hant-TW' })).toBe('zh')
    expect(mapHostLocale({ value: { preference: 'zh_Hans' } })).toBe('zh')
    expect(mapHostLocale('en-US')).toBe('en')
    expect(mapHostLocale({ preference: 'fr-FR' })).toBe('en')
  })

  it('registers both dictionaries, adopts changes, and disposes every Host seat', () => {
    const registered: Array<[string, string, Readonly<Record<string, string>>]> = []
    const released: string[] = []
    let listener: ((value?: HostLocaleValue) => void) | undefined
    const host: HostLocalePort = {
      locale: {
        getLocale: () => ({ active: 'en-GB' }),
        register: (namespace, locale, dict) => {
          registered.push([namespace, locale, dict])
          return () => { released.push(`dict:${locale}`) }
        },
      },
      on: (event, fn) => {
        expect(event).toBe('locale/change')
        listener = fn
        return () => { released.push('event') }
      },
    }

    const dispose = attachHostLocale(host)
    expect(registered).toEqual([
      [SIDECAR_LOCALE_NAMESPACE, 'zh', dictionaries.zh],
      [SIDECAR_LOCALE_NAMESPACE, 'en', dictionaries.en],
    ])
    expect(getLocale()).toBe('en')

    listener?.({ active: 'zh-CN' })
    expect(getLocale()).toBe('zh')

    dispose()
    dispose()
    expect(released).toEqual(['event', 'dict:en', 'dict:zh'])
  })

  it('leases one service across Host ports and re-homes events to a survivor', () => {
    const registered: string[] = []
    const registerDisposals = { zh: 0, en: 0 }
    let current: HostLocaleValue = { active: 'en-US' }
    let listenerA: ((value?: HostLocaleValue) => void) | undefined
    let listenerB: ((value?: HostLocaleValue) => void) | undefined
    let eventDisposalsA = 0
    let eventDisposalsB = 0
    const service: HostLocaleService = {
      getLocale: () => current,
      register: (_namespace, locale) => {
        registered.push(locale)
        return () => {
          registerDisposals[locale as keyof typeof registerDisposals] += 1
        }
      },
    }
    const hostA: HostLocalePort = {
      locale: service,
      on: (_event, listener) => {
        listenerA = listener
        return () => { eventDisposalsA += 1 }
      },
    }
    const hostB: HostLocalePort = {
      locale: service,
      on: (_event, listener) => {
        listenerB = listener
        return () => { eventDisposalsB += 1 }
      },
    }

    const disposeA = attachHostLocale(hostA)
    const disposeB = attachHostLocale(hostB)
    expect(registered).toEqual(['zh', 'en'])
    expect(listenerA).toBeTypeOf('function')
    expect(listenerB).toBeTypeOf('function')
    expect(eventDisposalsA).toBe(1)
    expect(getLocale()).toBe('en')

    disposeA()
    disposeA()
    expect(eventDisposalsA).toBe(1)
    expect(registerDisposals).toEqual({ zh: 0, en: 0 })
    expect(listenerB).toBeTypeOf('function')

    current = { active: 'zh-CN' }
    listenerB?.()
    expect(getLocale()).toBe('zh')
    listenerB?.({ active: 'en-GB' })
    expect(getLocale()).toBe('en')

    disposeB()
    expect(eventDisposalsB).toBe(1)
    expect(registerDisposals).toEqual({ zh: 1, en: 1 })
    expect(getLocale()).toBe(BASE_LOCALE)
  })

  it('shares the lease registry through the global Symbol registry', async () => {
    const key = Symbol.for('@shendeguize/dsh-agent-sidecar/host-locale-leases')
    const symbols = globalThis as typeof globalThis & { [key: symbol]: unknown }
    const registry = symbols[key]
    const shape = registry as {
      leases?: Record<PropertyKey, unknown>
      owners?: { size?: unknown }
      activeOwner?: unknown
      detachEvents?: unknown
    }

    expect(Symbol.keyFor(key)).toBe('@shendeguize/dsh-agent-sidecar/host-locale-leases')
    expect(typeof shape.leases?.get).toBe('function')
    expect(typeof shape.leases?.set).toBe('function')
    expect(typeof shape.leases?.delete).toBe('function')
    expect(shape.owners?.size).toBe(0)
    expect(shape.activeOwner).toBeNull()
    expect(shape.detachEvents).toBeTypeOf('function')

    await vi.resetModules()
    await import('../src/client/locales/index.ts')
    expect(symbols[key]).toBe(registry)
  })

  it('keeps replacement service B active when service A releases first', async () => {
    let currentA: HostLocaleValue = { active: 'zh-CN' }
    let currentB: HostLocaleValue = { active: 'en-US' }
    let listenerA: ((value?: HostLocaleValue) => void) | undefined
    let listenerB: ((value?: HostLocaleValue) => void) | undefined
    const registrationsA: string[] = []
    const registrationsB: string[] = []
    const dictionaryDisposalsA = { zh: 0, en: 0 }
    const dictionaryDisposalsB = { zh: 0, en: 0 }
    let eventDisposalsA = 0
    let eventDisposalsB = 0
    const serviceA: HostLocaleService = {
      getLocale: () => currentA,
      register: (_namespace, locale) => {
        registrationsA.push(locale)
        return () => {
          dictionaryDisposalsA[locale as keyof typeof dictionaryDisposalsA] += 1
        }
      },
    }
    const serviceB: HostLocaleService = {
      getLocale: () => currentB,
      register: (_namespace, locale) => {
        registrationsB.push(locale)
        return () => {
          dictionaryDisposalsB[locale as keyof typeof dictionaryDisposalsB] += 1
        }
      },
    }
    const disposers: Array<() => void> = []

    try {
      const disposeA = attachHostLocale({
        locale: serviceA,
        on: (_event, listener) => {
          listenerA = listener
          return () => { eventDisposalsA += 1 }
        },
      })
      disposers.push(disposeA)
      expect(getLocale()).toBe('zh')

      await vi.resetModules()
      const bundleB = await import('../src/client/locales/index.ts')
      const disposeB = bundleB.attachHostLocale({
        locale: serviceB,
        on: (_event, listener) => {
          listenerB = listener
          return () => { eventDisposalsB += 1 }
        },
      })
      disposers.push(disposeB)
      expect(registrationsA).toEqual(['zh', 'en'])
      expect(registrationsB).toEqual(['zh', 'en'])
      expect(eventDisposalsA).toBe(1)
      expect(bundleB.getLocale()).toBe('en')

      currentA = { active: 'en-GB' }
      listenerA?.()
      expect(getLocale()).toBe('zh')
      expect(bundleB.getLocale()).toBe('en')

      disposeA()
      disposeA()
      expect(dictionaryDisposalsA).toEqual({ zh: 1, en: 1 })
      expect(dictionaryDisposalsB).toEqual({ zh: 0, en: 0 })
      expect(bundleB.getLocale()).toBe('en')
      expect(eventDisposalsB).toBe(0)

      currentB = { active: 'zh-Hant' }
      listenerB?.()
      expect(bundleB.getLocale()).toBe('zh')
      listenerB?.({ active: 'en-AU' })
      expect(bundleB.getLocale()).toBe('en')

      disposeB()
      disposeB()
      expect(eventDisposalsB).toBe(1)
      expect(dictionaryDisposalsB).toEqual({ zh: 1, en: 1 })
      expect(bundleB.getLocale()).toBe(BASE_LOCALE)
    } finally {
      for (const dispose of disposers.reverse()) dispose()
    }
  })

  it('re-homes from replacement service B to A when B releases first', async () => {
    let currentA: HostLocaleValue = { active: 'zh-CN' }
    const currentB: HostLocaleValue = { active: 'en-US' }
    let listenerA: ((value?: HostLocaleValue) => void) | undefined
    let listenerB: ((value?: HostLocaleValue) => void) | undefined
    const dictionaryDisposalsA = { zh: 0, en: 0 }
    const dictionaryDisposalsB = { zh: 0, en: 0 }
    let eventAttachmentsA = 0
    let eventDisposalsA = 0
    let eventDisposalsB = 0
    const serviceA: HostLocaleService = {
      getLocale: () => currentA,
      register: (_namespace, locale) => () => {
        dictionaryDisposalsA[locale as keyof typeof dictionaryDisposalsA] += 1
      },
    }
    const serviceB: HostLocaleService = {
      getLocale: () => currentB,
      register: (_namespace, locale) => () => {
        dictionaryDisposalsB[locale as keyof typeof dictionaryDisposalsB] += 1
      },
    }
    const disposers: Array<() => void> = []

    try {
      const disposeA = attachHostLocale({
        locale: serviceA,
        on: (_event, listener) => {
          eventAttachmentsA += 1
          listenerA = listener
          return () => { eventDisposalsA += 1 }
        },
      })
      disposers.push(disposeA)

      await vi.resetModules()
      const bundleB = await import('../src/client/locales/index.ts')
      const disposeB = bundleB.attachHostLocale({
        locale: serviceB,
        on: (_event, listener) => {
          listenerB = listener
          return () => { eventDisposalsB += 1 }
        },
      })
      disposers.push(disposeB)
      expect(bundleB.getLocale()).toBe('en')
      expect(eventAttachmentsA).toBe(1)
      expect(eventDisposalsA).toBe(1)

      currentA = { active: 'en-GB' }
      disposeB()
      disposeB()
      expect(eventDisposalsB).toBe(1)
      expect(dictionaryDisposalsB).toEqual({ zh: 1, en: 1 })
      expect(eventAttachmentsA).toBe(2)
      expect(getLocale()).toBe('en')
      expect(bundleB.getLocale()).toBe('en')

      listenerB?.({ active: 'zh-CN' })
      expect(getLocale()).toBe('en')
      expect(bundleB.getLocale()).toBe('en')
      currentA = { active: 'zh-Hans' }
      listenerA?.()
      expect(getLocale()).toBe('zh')

      disposeA()
      disposeA()
      expect(eventDisposalsA).toBe(2)
      expect(dictionaryDisposalsA).toEqual({ zh: 1, en: 1 })
      expect(getLocale()).toBe(BASE_LOCALE)
    } finally {
      for (const dispose of disposers.reverse()) dispose()
    }
  })

  it('moves a shared lease between bundle-local locale owners', async () => {
    const registered: string[] = []
    const registerDisposals = { zh: 0, en: 0 }
    let current: HostLocaleValue = { active: 'en-US' }
    let listenerOld: ((value?: HostLocaleValue) => void) | undefined
    let listenerNew: ((value?: HostLocaleValue) => void) | undefined
    let oldEventAttachments = 0
    let oldEventDisposals = 0
    let newEventAttachments = 0
    let newEventDisposals = 0
    const service: HostLocaleService = {
      getLocale: () => current,
      register: (_namespace, locale) => {
        registered.push(locale)
        return () => {
          registerDisposals[locale as keyof typeof registerDisposals] += 1
        }
      },
    }
    const oldHost: HostLocalePort = {
      locale: service,
      on: (_event, listener) => {
        oldEventAttachments += 1
        listenerOld = listener
        return () => { oldEventDisposals += 1 }
      },
    }
    const newHost: HostLocalePort = {
      locale: service,
      on: (_event, listener) => {
        newEventAttachments += 1
        listenerNew = listener
        return () => { newEventDisposals += 1 }
      },
    }
    const disposers: Array<() => void> = []

    try {
      const disposeOld = attachHostLocale(oldHost)
      disposers.push(disposeOld)
      expect(getLocale()).toBe('en')
      listenerOld?.({ active: 'zh-CN' })
      expect(getLocale()).toBe('zh')

      await vi.resetModules()
      const newBundle = await import('../src/client/locales/index.ts')
      expect(newBundle.getLocale()).toBe('zh')

      current = { active: 'en-GB' }
      const disposeNew = newBundle.attachHostLocale(newHost)
      disposers.push(disposeNew)
      expect(registered).toEqual(['zh', 'en'])
      expect(oldEventAttachments).toBe(1)
      expect(oldEventDisposals).toBe(1)
      expect(newEventAttachments).toBe(1)
      expect(getLocale()).toBe('zh')
      expect(newBundle.getLocale()).toBe('en')

      current = { active: 'zh-Hant' }
      listenerNew?.()
      expect(newBundle.getLocale()).toBe('zh')
      expect(getLocale()).toBe('zh')

      current = { active: 'en-US' }
      disposeNew()
      disposeNew()
      expect(newEventDisposals).toBe(1)
      expect(oldEventAttachments).toBe(2)
      expect(getLocale()).toBe('en')
      expect(newBundle.getLocale()).toBe('zh')
      expect(registerDisposals).toEqual({ zh: 0, en: 0 })

      disposeOld()
      disposeOld()
      expect(oldEventDisposals).toBe(2)
      expect(registerDisposals).toEqual({ zh: 1, en: 1 })
      expect(getLocale()).toBe(BASE_LOCALE)
    } finally {
      for (const dispose of disposers.reverse()) dispose()
    }
  })

  it('degrades quietly when Host capabilities are absent or throw', () => {
    const seen: string[] = []
    expect(() => {
      const dispose = bridgeHostLocale(
        {
          locale: {
            getLocale: () => { throw new Error('unavailable') },
            register: () => { throw new Error('unavailable') },
          },
          on: () => { throw new Error('unavailable') },
        },
        {
          namespace: 'test',
          dictionaries: { zh: {}, en: {} },
          onLocale: locale => { seen.push(locale) },
        },
      )
      dispose()
      attachHostLocale({
        locale: {
          getLocale: () => { throw new Error('unavailable') },
          register: () => { throw new Error('unavailable') },
        },
        on: () => { throw new Error('unavailable') },
      })()
      attachHostLocale(undefined)()
    }).not.toThrow()
    expect(seen).toEqual([])
    expect(getLocale()).toBe(BASE_LOCALE)
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
