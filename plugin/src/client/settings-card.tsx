/**
 * Agent Sidecar settings card (browser half, T2.3).
 *
 * MECHANISM (source-audit conclusion): the dsh settings pane does NOT
 * auto-render a form from the host Config schema. The plugin-configuration
 * tab dispatches the keyed `settings.plugin.item` slot per Host-served
 * settings namespace and "a card draws its own internals; the tab only
 * decides which namespaces to dispatch and stacks what comes back"
 * (harness `packages/client/ui-settings-plugins/src/client/slot-contract.ts`;
 * unclaimed namespaces render nothing per the adding-a-settings-card
 * cookbook). This card therefore draws the live form UI for
 * inject.enabled/defaultMode, analysis enabled/provider/model and
 * ui.timeWindowHours/showDead, plus read-only deployment guidance for the
 * profile-owned daemon/sidecar/stream/skill groups, daemon status/retry, and
 * the injection safety note.
 *
 * WIRING CONTRACT (T2.4): the card is fully controlled and presentational.
 * `values` is the staged draft owned by the wiring controller; every edit
 * reports through `onChange(field, value)` and the controller must echo the
 * staged value back through `values` (the shipped ui-settings-plugins cards
 * follow the same staged-edit split). Save/discard/retry are plain
 * callbacks. Registration into `settings.plugin.item` (keyed by the Host
 * settings namespace) also belongs to the wiring half — this module exports
 * the component and its props contract only.
 *
 * Runtime ownership is deliberately explicit: daemon/sidecar/stream values
 * and skill.provide are read from the profile cordis.patch config when the
 * plugin is applied, so this card shows those groups as read-only guidance.
 * inject/analysis/ui remain editable because their effective settings are
 * consumed live.
 *
 * The card renders as an `<li>` because the plugin-configuration tab stacks
 * cards in a list (shipped PluginCard precedent). Chrome (disclosure
 * header, unsaved pill, save/discard footer) mirrors the shipped card so
 * the sidecar card reads native next to first-party ones.
 */

import {
  Button,
  IconChevronDownOutline14,
  Input,
  Pill,
} from '@deepseek-ai/dsh-client-ui-primitives'
import { useId, useState } from 'react'
import type { ReactNode } from 'react'
import { t as defaultT } from './locales/index.ts'
import type { SidecarLocaleKey } from './locales/index.ts'
import {
  NumberField,
  SelectField,
  ToggleField,
} from './settings-fields.tsx'
import { surfaceProps } from './theme/parts.ts'
import css from './settings-card.module.css'

/**
 * Daemon supervisor state as surfaced to the browser. Mirrors
 * `SupervisorState` in src/supervisor.ts verbatim; restated here because
 * host and client are separate TS programs (tsconfig.host excludes
 * src/client and vice versa) and the client bundle must not import host
 * modules.
 */
export type SidecarDaemonState =
  | 'probe'
  | 'adopted'
  | 'defer'
  | 'reprobe'
  | 'hosting'
  | 'hosted'
  | 'backoff'
  | 'failed'

/** Daemon status block input (shape matches the supervisor's ping-derived view). */
export interface SidecarDaemonStatus {
  state: SidecarDaemonState
  /** Daemon pid, when a ping succeeded. */
  pid?: number
  /** Daemon version, when a ping succeeded. */
  version?: string
}

/**
 * The staged form values — one flat field per key item of the host Config
 * schema (grouping mirrors src/config.ts).
 */
export interface SettingsCardValues {
  /** daemon.policy */
  daemonPolicy: 'adopt-or-host' | 'adopt-only' | 'off'
  /** daemon.backoffLimit (integer ≥ 1) */
  daemonBackoffLimit: number
  /**
   * sidecar.command, carried as one whitespace-joined line so existing
   * settings documents still round-trip without loss.
   */
  sidecarCommand: string
  /** sidecar.runtimeDir ('' = default ~/.agent_sidecar) */
  sidecarRuntimeDir: string
  /** stream.reconcileActiveMs (integer ≥ 100) */
  streamReconcileActiveMs: number
  /** stream.reconcileIdleMs (integer ≥ 100) */
  streamReconcileIdleMs: number
  /** inject.enabled */
  injectEnabled: boolean
  /** inject.defaultMode */
  injectDefaultMode: 'queue' | 'steer'
  /** analysis.enabled */
  analysisEnabled: boolean
  /**
   * analysis.provider / analysis.model ('' = reuse the host default model).
   * Carried through the staged values for save round-trip fidelity (a group
   * write is a COMPLETE analysis object — dropping either would wipe an
   * explicit configuration). The UI accepts only both blank or both non-blank.
   */
  analysisProvider: string
  analysisModel: string
  /** ui.timeWindowHours (integer ≥ 1) */
  uiTimeWindowHours: number
  /** ui.showDead */
  uiShowDead: boolean
  /** archive.auto */
  archiveAuto: boolean
  /** archive.autoAfterHours (integer 1…720) */
  archiveAutoAfterHours: number
  /** skill.provide */
  skillProvide: boolean
}

/**
 * The policy the REACHED daemon actually applies, as reported by its own
 * status. Distinct from the staged config on purpose: an adopted or
 * service-managed daemon owns the policy it was started with, so the card
 * must show what is in force rather than what this plugin would ask for.
 */
export interface SidecarArchivePolicyStatus {
  auto: boolean
  autoAfterSeconds: number
}

/**
 * Translate signature the card consumes: the module-local `t` by default,
 * or (same shape) a slot-injected / ctx.locale-bound seat supplied by the
 * wiring half.
 */
export type SettingsTranslate = (
  key: SidecarLocaleKey,
  params?: Record<string, unknown>,
) => string

export interface SettingsCardProps {
  /** Staged form values (owned and echoed back by the wiring controller). */
  values: SettingsCardValues
  /** Report one staged edit. */
  onChange: <K extends keyof SettingsCardValues>(
    field: K,
    value: SettingsCardValues[K],
  ) => void
  /** Write every staged edit. */
  onSave: () => void
  /** Drop every staged edit. */
  onDiscard: () => void
  /** False disables every control and shows the read-only note. */
  writable: boolean
  /** Whether unsaved edits stand. */
  dirty: boolean
  /** Whether a save is in flight. */
  saving: boolean
  /** Whether the last save failed. */
  saveFailed?: boolean
  /** Daemon status block; omitted hides the row. */
  daemon?: SidecarDaemonStatus
  /**
   * Policy in force on the reached daemon. Null/omitted means no daemon has
   * answered yet (or it predates the archive ops), in which case the card
   * says so instead of echoing the staged config back as if it were live.
   */
  archivePolicy?: SidecarArchivePolicyStatus | null
  /** Retry hosting after the supervisor tripped `failed`. */
  onDaemonRetry?: () => void
  /** Documentation link target; omitted hides the link. */
  docsUrl?: string
  /** Locale seat override; defaults to the module-local table. */
  t?: SettingsTranslate
  /** Initial disclosure state; omitted keeps the production card collapsed. */
  defaultOpen?: boolean
}

const DAEMON_STATE_KEY: Record<SidecarDaemonState, SidecarLocaleKey> = {
  'probe': 'settings.daemonStateProbe',
  'adopted': 'settings.daemonStateAdopted',
  'defer': 'settings.daemonStateDefer',
  'reprobe': 'settings.daemonStateReprobe',
  'hosting': 'settings.daemonStateHosting',
  'hosted': 'settings.daemonStateHosted',
  'backoff': 'settings.daemonStateBackoff',
  'failed': 'settings.daemonStateFailed',
}

/** Dot classes per daemon state: healthy / transitional / tripped. */
function statusDotClass(state: SidecarDaemonState): string {
  if (state === 'adopted' || state === 'hosted') return `${css['statusDot']} ${css['statusOk']}`
  if (state === 'failed') return `${css['statusDot']} ${css['statusError']}`
  return css['statusDot'] ?? ''
}

/**
 * Seconds → hours for the live-policy readout. One decimal, trailing zero
 * dropped: the daemon accepts any duration, so a policy of 90 minutes must
 * not be rounded into a lie about 2 hours.
 */
export function formatPolicyHours(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0'
  const hours = seconds / 3600
  return hours >= 10 || Number.isInteger(hours)
    ? String(Math.round(hours))
    : String(Math.round(hours * 10) / 10)
}

interface SectionProps {
  title: string
  children: ReactNode
}

function Section(props: SectionProps): ReactNode {
  return (
    <section className={css['section']}>
      <h3 className={css['sectionTitle']}>{props.title}</h3>
      {props.children}
    </section>
  )
}

export type AnalysisRouteKind = 'host-default' | 'explicit' | 'partial'

export interface AnalysisRouteResolution {
  kind: AnalysisRouteKind
  provider: string
  model: string
}

/** Trim one edited route token exactly as the host does before resolving it. */
export function normalizeAnalysisRouteField(value: string): string {
  return value.trim()
}

/**
 * Resolve the analysis route contract shared with the host:
 * both blank means host default, both set means explicit, and a partial pair
 * is invalid settings UI input (the runtime would otherwise silently fall
 * back to the host default).
 */
export function resolveAnalysisRoute(
  provider: string,
  model: string,
): AnalysisRouteResolution {
  const normalizedProvider = normalizeAnalysisRouteField(provider)
  const normalizedModel = normalizeAnalysisRouteField(model)
  const hasProvider = normalizedProvider !== ''
  const hasModel = normalizedModel !== ''
  return {
    kind: hasProvider === hasModel
      ? hasProvider ? 'explicit' : 'host-default'
      : 'partial',
    provider: normalizedProvider,
    model: normalizedModel,
  }
}

interface AnalysisRouteFieldProps {
  label: string
  hint: string
  value: string
  placeholder: string
  disabled: boolean
  invalid: boolean
  onCommit: (value: string) => void
}

function AnalysisRouteField(props: AnalysisRouteFieldProps): ReactNode {
  const id = useId()
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <Input
        id={id}
        className={`${css['input']} ${props.invalid ? css['inputInvalid'] : ''}`}
        type="text"
        value={props.value}
        placeholder={props.placeholder}
        disabled={props.disabled}
        {...props.invalid ? { 'aria-invalid': true } : {}}
        onChange={(event) => { props.onCommit(normalizeAnalysisRouteField(event.target.value)) }}
      />
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

/**
 * Render the Agent Sidecar settings card.
 * @param props - staged values, form state, and the wiring callbacks.
 * @returns the card.
 */
export function SettingsCard(props: SettingsCardProps): ReactNode {
  const [open, setOpen] = useState(props.defaultOpen ?? false)
  const t = props.t ?? defaultT
  const { values } = props
  const disabled = !props.writable || props.saving
  const title = t('settings.cardTitle')
  const analysisRoute = resolveAnalysisRoute(
    values.analysisProvider,
    values.analysisModel,
  )
  const analysisRouteInvalid = analysisRoute.kind === 'partial'
  const analysisRouteStatus = analysisRoute.kind === 'host-default'
    ? t('settings.analysisRouteHostDefault')
    : analysisRoute.kind === 'explicit'
      ? t('settings.analysisRouteExplicit', {
          provider: analysisRoute.provider,
          model: analysisRoute.model,
        })
      : t('settings.analysisRoutePartial')

  // What the reached daemon is really doing, in its own units. A policy
  // this plugin staged but no daemon has picked up yet must not read as
  // being in force.
  const livePolicy = props.archivePolicy ?? null
  const archiveLive =
    livePolicy === null
      ? t('settings.archiveLiveUnknown')
      : livePolicy.auto
        ? t('settings.archiveLiveOn', { hours: formatPolicyHours(livePolicy.autoAfterSeconds) })
        : t('settings.archiveLiveOff')

  const daemonNote = props.daemon?.state === 'defer'
    ? t('settings.daemonDeferNote')
    : props.daemon?.state === 'failed'
      ? t('settings.daemonFailedNote')
      : undefined
  const cardClassName = `${css['card']} ${open ? css['cardOpen'] : ''}`

  return (
    <li {...surfaceProps('settings-card', cardClassName)}>
      <button
        type="button"
        className={css['header']}
        aria-expanded={open}
        aria-label={`${t(open ? 'settings.collapse' : 'settings.expand')}: ${title}`}
        onClick={() => { setOpen(!open) }}
      >
        <span className={css['headText']}>
          <span className={css['name']}>{title}</span>
          <span className={css['description']}>{t('settings.cardDescription')}</span>
        </span>
        {props.dirty ? <Pill className={css['pending']}>{t('settings.unsaved')}</Pill> : null}
        <IconChevronDownOutline14
          className={`${css['chevron']} ${open ? css['chevronOpen'] : ''}`}
        />
      </button>
      {open
        ? (
          <div className={css['body']}>
            {!props.writable
              ? <p className={css['readOnly']} role="status">{t('settings.readOnly')}</p>
              : null}

            <Section title={t('settings.sectionDaemon')}>
              {props.daemon
                ? (
                  <div className={css['field']}>
                    <span className={css['label']}>{t('settings.daemonStatusLabel')}</span>
                    <div className={css['statusRow']}>
                      <span className={statusDotClass(props.daemon.state)} aria-hidden />
                      <span className={css['statusText']}>
                        {t(DAEMON_STATE_KEY[props.daemon.state])}
                      </span>
                      {props.daemon.pid !== undefined && props.daemon.version !== undefined
                        ? (
                          <span className={css['statusMeta']}>
                            {t('settings.daemonPidVersion', {
                              pid: props.daemon.pid,
                              version: props.daemon.version,
                            })}
                          </span>
                        )
                        : null}
                      {props.daemon.state === 'failed' && props.onDaemonRetry !== undefined
                        ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            className={css['retry']}
                            onClick={props.onDaemonRetry}
                          >
                            {t('settings.daemonRetry')}
                          </Button>
                        )
                        : null}
                    </div>
                    {daemonNote !== undefined
                      ? <p className={css['note']}>{daemonNote}</p>
                      : null}
                  </div>
                )
                : null}
              <p className={css['note']}>{t('settings.daemonProfileNote')}</p>
            </Section>

            <Section title={t('settings.sectionSidecar')}>
              <p className={css['note']}>{t('settings.sidecarProfileNote')}</p>
            </Section>

            <Section title={t('settings.sectionStream')}>
              <p className={css['note']}>{t('settings.streamProfileNote')}</p>
            </Section>

            <Section title={t('settings.sectionInject')}>
              <p className={css['note']}>{t('settings.liveEffectNote')}</p>
              <p className={css['note']}>{t('settings.injectSafetyNote')}</p>
              <ToggleField
                label={t('settings.injectEnabledLabel')}
                hint={t('settings.injectEnabledHint')}
                checked={values.injectEnabled}
                disabled={disabled}
                onCommit={(checked) => { props.onChange('injectEnabled', checked) }}
              />
              <SelectField
                label={t('settings.injectDefaultModeLabel')}
                hint={t('settings.injectDefaultModeHint')}
                value={values.injectDefaultMode}
                disabled={disabled}
                options={[
                  { value: 'queue', label: t('settings.injectModeQueue') },
                  { value: 'steer', label: t('settings.injectModeSteer') },
                ]}
                onCommit={(value) => {
                  props.onChange('injectDefaultMode', value as SettingsCardValues['injectDefaultMode'])
                }}
              />
            </Section>

            <Section title={t('settings.sectionAnalysis')}>
              <p className={css['note']}>{t('settings.liveEffectNote')}</p>
              <ToggleField
                label={t('settings.analysisEnabledLabel')}
                hint={t('settings.analysisEnabledHint')}
                checked={values.analysisEnabled}
                disabled={disabled}
                onCommit={(checked) => { props.onChange('analysisEnabled', checked) }}
              />
              <AnalysisRouteField
                label={t('settings.analysisProviderLabel')}
                hint={t('settings.analysisProviderHint')}
                value={values.analysisProvider}
                placeholder={t('settings.analysisProviderPlaceholder')}
                disabled={disabled}
                invalid={analysisRouteInvalid && analysisRoute.provider === ''}
                onCommit={(value) => { props.onChange('analysisProvider', value) }}
              />
              <AnalysisRouteField
                label={t('settings.analysisModelLabel')}
                hint={t('settings.analysisModelHint')}
                value={values.analysisModel}
                placeholder={t('settings.analysisModelPlaceholder')}
                disabled={disabled}
                invalid={analysisRouteInvalid && analysisRoute.model === ''}
                onCommit={(value) => { props.onChange('analysisModel', value) }}
              />
              <p
                className={analysisRouteInvalid ? css['invalidHint'] : css['note']}
                {...analysisRouteInvalid ? { role: 'alert' } : {}}
              >
                {analysisRouteStatus}
              </p>
            </Section>

            <Section title={t('settings.sectionUi')}>
              <p className={css['note']}>{t('settings.liveEffectNote')}</p>
              <NumberField
                label={t('settings.uiTimeWindowHoursLabel')}
                hint={t('settings.uiTimeWindowHoursHint')}
                invalidHint={t('settings.invalidNumber', { min: 1 })}
                min={1}
                value={values.uiTimeWindowHours}
                disabled={disabled}
                onCommit={(value) => { props.onChange('uiTimeWindowHours', value) }}
              />
              <ToggleField
                label={t('settings.uiShowDeadLabel')}
                hint={t('settings.uiShowDeadHint')}
                checked={values.uiShowDead}
                disabled={disabled}
                onCommit={(checked) => { props.onChange('uiShowDead', checked) }}
              />
            </Section>

            <Section title={t('settings.sectionArchive')}>
              <p className={css['note']}>{t('settings.archiveExplain')}</p>
              <div className={css['field']}>
                <span className={css['label']}>{t('settings.archiveLiveLabel')}</span>
                <span className={css['statusText']} data-testid="agent-sidecar-settings-archive-live">
                  {archiveLive}
                </span>
              </div>
              <ToggleField
                label={t('settings.archiveAutoLabel')}
                hint={t('settings.archiveAutoHint')}
                checked={values.archiveAuto}
                disabled={disabled}
                onCommit={(checked) => { props.onChange('archiveAuto', checked) }}
              />
              <NumberField
                label={t('settings.archiveAfterHoursLabel')}
                hint={t('settings.archiveAfterHoursHint')}
                invalidHint={t('settings.invalidNumber', { min: 1 })}
                min={1}
                value={values.archiveAutoAfterHours}
                disabled={disabled || !values.archiveAuto}
                onCommit={(value) => { props.onChange('archiveAutoAfterHours', value) }}
              />
              <p className={css['note']}>{t('settings.archiveHostedOnlyNote')}</p>
            </Section>

            <Section title={t('settings.sectionSkill')}>
              <p className={css['note']}>{t('settings.skillRestartNote')}</p>
            </Section>

            <div className={css['footer']}>
              {props.docsUrl !== undefined
                ? (
                  <a
                    className={css['docs']}
                    href={props.docsUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('settings.docsLink')}
                  </a>
                )
                : null}
              {props.saveFailed === true
                ? <p className={css['failed']} role="status">{t('settings.saveFailed')}</p>
                : <span className={css['spacer']} />}
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!props.dirty || props.saving}
                onClick={props.onDiscard}
              >
                {t('settings.discard')}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="primary"
                disabled={!props.dirty || props.saving || !props.writable || analysisRouteInvalid}
                onClick={() => {
                  if (!analysisRouteInvalid) props.onSave()
                }}
              >
                {t(props.saving ? 'settings.saving' : 'settings.save')}
              </Button>
            </div>
          </div>
        )
        : null}
    </li>
  )
}
