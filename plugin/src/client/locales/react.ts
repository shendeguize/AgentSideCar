import { useSyncExternalStore } from 'react'
import { getLocale, subscribeLocale, type SidecarLocale } from './index.ts'

/**
 * Subscribe the calling React root to the module-owned active locale.
 * Reading translations remains late-bound through `t` and locale facades.
 */
export function useActiveLocale(): SidecarLocale {
  return useSyncExternalStore(subscribeLocale, getLocale, getLocale)
}
