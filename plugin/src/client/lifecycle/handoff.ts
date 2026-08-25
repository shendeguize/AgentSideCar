export type ResourceDisposer = () => void

/** Narrow clock port for deterministic handoff tests. */
export interface HandoffScheduler {
  setTimeout(callback: () => void, delayMs: number): unknown
  clearTimeout(handle: unknown): void
}

export interface HandoffOptions {
  isCollision(error: unknown): boolean
  onError(error: unknown): void
  onTimeout(): void
  scheduler?: HandoffScheduler
}

const DEFAULT_DELAYS_MS = [8, 16, 32, 64, 128, 256, 496] as const

const defaultScheduler: HandoffScheduler = {
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}

/**
 * Acquire a registry resource now, or briefly wait for an overlapping old
 * fiber to release it. The returned lease owns both waiting and acquisition.
 */
export function acquireWithHandoff(
  register: () => ResourceDisposer | undefined,
  options: HandoffOptions,
): ResourceDisposer {
  const scheduler = options.scheduler ?? defaultScheduler
  let stopped = false
  let attempts = 0
  let delayIndex = 0
  let timer: unknown
  let acquired: ResourceDisposer | undefined

  const finishError = (error: unknown): void => {
    stopped = true
    options.onError(error)
  }

  const attempt = (): void => {
    if (stopped) return
    timer = undefined
    attempts += 1
    try {
      acquired = register()
    } catch (error) {
      if (!options.isCollision(error)) {
        finishError(error)
        return
      }
    }
    if (acquired !== undefined) return
    if (attempts >= 8 || delayIndex >= DEFAULT_DELAYS_MS.length) {
      stopped = true
      options.onTimeout()
      return
    }
    const delay = DEFAULT_DELAYS_MS[delayIndex++]
    try {
      timer = scheduler.setTimeout(attempt, delay!)
    } catch (error) {
      finishError(error)
    }
  }

  attempt()
  return () => {
    if (stopped) return
    stopped = true
    if (timer !== undefined) scheduler.clearTimeout(timer)
    const dispose = acquired
    acquired = undefined
    dispose?.()
  }
}

/** Only known duplicate-registry diagnostics are eligible for handoff. */
export function isRegistrationCollision(error: unknown): boolean {
  return error instanceof Error && /\b(?:duplicate|already registered)\b/i.test(error.message)
}
