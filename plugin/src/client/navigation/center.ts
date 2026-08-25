/** Callback port used by UI entry points that open Agent Center. */
export type CenterNavigation = () => boolean

/** Observable state port bound to the shell overlay at the composition root. */
export interface CenterNavigationStore {
  readonly open: CenterNavigation
  close(): void
  subscribe(listener: () => void): () => void
  getSnapshot(): boolean
}

/**
 * Create one DOM-free navigation source for every Agent Center entry point.
 * Opening is always accepted; a shell overlay that mounts later observes the
 * retained snapshot instead of losing the request.
 */
export function createCenterNavigation(): CenterNavigationStore {
  let isOpen = false
  const listeners = new Set<() => void>()
  const notify = (): void => {
    for (const listener of [...listeners]) listener()
  }

  const open: CenterNavigation = () => {
    if (!isOpen) {
      isOpen = true
      notify()
    }
    return true
  }

  return {
    open,
    close: () => {
      if (!isOpen) return
      isOpen = false
      notify()
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    getSnapshot: () => isOpen,
  }
}
