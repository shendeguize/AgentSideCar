/**
 * Pure conversion layer between the host Config wire shape and the settings
 * card's flat form values (T2.4 wiring glue; no React, no I/O, node-testable).
 *
 * `SidecarConfigView` hand-mirrors the host `Config` interface
 * (src/config.ts) the same way api.ts mirrors the route wire types: host and
 * client are separate TS programs, so the client restates the shape it reads
 * off the settings scope (`ctx.settingsScope.bind({namespace}).getSnapshot()
 * .value` carries the schema-resolved section, which IS this shape).
 *
 * Write granularity: the browser settings scope's `set(field, value)` writes
 * exactly one top-level path segment (`{op:'set', path:[field]}` — verified
 * against harness ui-settings settings-scope.ts), so saving works per
 * top-level GROUP: {@link diffGroups} emits one complete group object per
 * group whose staged values differ from the resolved ones. Writing a whole
 * group marks all its fields user-overridden — the settings service treats
 * presence in the user layer as the override marker regardless of value,
 * which is its documented semantics, not a distortion.
 *
 * @module
 */

import type { SettingsCardValues } from './settings-card.tsx'

/** Mirror of the host `Config` (src/config.ts); see the module doc. */
export interface SidecarConfigView {
  daemon: { policy: 'adopt-or-host' | 'adopt-only' | 'off'; backoffLimit: number }
  sidecar: { command: string[]; runtimeDir: string }
  stream: { reconcileActiveMs: number; reconcileIdleMs: number }
  inject: { enabled: boolean; defaultMode: 'queue' | 'steer' }
  analysis: { enabled: boolean; provider: string; model: string }
  ui: { timeWindowHours: number; showDead: boolean }
  skill: { provide: boolean }
}

/** Schema defaults of src/config.ts (fallback while the scope is not ready). */
export const DEFAULT_CONFIG_VIEW: SidecarConfigView = {
  daemon: { policy: 'adopt-or-host', backoffLimit: 5 },
  sidecar: { command: ['agent-sidecar'], runtimeDir: '' },
  stream: { reconcileActiveMs: 2000, reconcileIdleMs: 10000 },
  inject: { enabled: false, defaultMode: 'queue' },
  analysis: { enabled: false, provider: '', model: '' },
  ui: { timeWindowHours: 24, showDead: false },
  skill: { provide: false },
}

/** Whitespace-join an argv for the card's single-line command field. */
export function joinCommand(argv: readonly string[]): string {
  return argv.join(' ')
}

/** Split the card's command line back into argv (whitespace, empty dropped). */
export function splitCommand(line: string): string[] {
  return line.split(/\s+/).filter((part) => part !== '')
}

/** Config wire shape → the card's flat staged-form values. */
export function configToValues(config: SidecarConfigView): SettingsCardValues {
  return {
    daemonPolicy: config.daemon.policy,
    daemonBackoffLimit: config.daemon.backoffLimit,
    sidecarCommand: joinCommand(config.sidecar.command),
    sidecarRuntimeDir: config.sidecar.runtimeDir,
    streamReconcileActiveMs: config.stream.reconcileActiveMs,
    streamReconcileIdleMs: config.stream.reconcileIdleMs,
    injectEnabled: config.inject.enabled,
    injectDefaultMode: config.inject.defaultMode,
    analysisEnabled: config.analysis.enabled,
    analysisProvider: config.analysis.provider,
    analysisModel: config.analysis.model,
    uiTimeWindowHours: config.ui.timeWindowHours,
    uiShowDead: config.ui.showDead,
    skillProvide: config.skill.provide,
  }
}

/** Flat card values → the grouped config wire shape. */
export function valuesToConfigView(values: SettingsCardValues): SidecarConfigView {
  return {
    daemon: { policy: values.daemonPolicy, backoffLimit: values.daemonBackoffLimit },
    sidecar: {
      command: splitCommand(values.sidecarCommand),
      runtimeDir: values.sidecarRuntimeDir,
    },
    stream: {
      reconcileActiveMs: values.streamReconcileActiveMs,
      reconcileIdleMs: values.streamReconcileIdleMs,
    },
    inject: { enabled: values.injectEnabled, defaultMode: values.injectDefaultMode },
    analysis: {
      enabled: values.analysisEnabled,
      provider: values.analysisProvider,
      model: values.analysisModel,
    },
    ui: { timeWindowHours: values.uiTimeWindowHours, showDead: values.uiShowDead },
    skill: { provide: values.skillProvide },
  }
}

/** Field-wise equality over the flat card values (argv compared as joined text). */
export function cardValuesEqual(a: SettingsCardValues, b: SettingsCardValues): boolean {
  return (Object.keys(a) as Array<keyof SettingsCardValues>).every(
    (key) => a[key] === b[key],
  )
}

/** One pending settings write: a complete top-level group object. */
export interface GroupPatch {
  group: keyof SidecarConfigView
  patch: Record<string, unknown>
}

/**
 * Diff two card-value sets into per-group writes (see module doc for why the
 * write unit is a whole group). Order is the Config declaration order.
 */
export function diffGroups(
  current: SettingsCardValues,
  target: SettingsCardValues,
): GroupPatch[] {
  const from = valuesToConfigView(current)
  const to = valuesToConfigView(target)
  const patches: GroupPatch[] = []
  for (const group of Object.keys(to) as Array<keyof SidecarConfigView>) {
    if (JSON.stringify(from[group]) !== JSON.stringify(to[group])) {
      patches.push({ group, patch: { ...to[group] } })
    }
  }
  return patches
}
