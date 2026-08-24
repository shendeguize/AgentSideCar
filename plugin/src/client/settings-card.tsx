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
 * cookbook). This card therefore carries the form UI for the key items of
 * the host Config (src/config.ts): daemon.policy/backoffLimit,
 * sidecar.command/runtimeDir, stream.reconcileActiveMs/IdleMs,
 * inject.enabled/defaultMode, analysis.enabled, ui.timeWindowHours/showDead,
 * skill.provide — plus the daemon status/retry row and the injection safety
 * note the design doc (§4.a/§5.3/§6) puts on the settings surface.
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
 * The card renders as an `<li>` because the plugin-configuration tab stacks
 * cards in a list (shipped PluginCard precedent). Chrome (disclosure
 * header, unsaved pill, save/discard footer) mirrors the shipped card so
 * the sidecar card reads native next to first-party ones.
 */

import { useEffect, useId, useState } from 'react'
import type { ReactNode } from 'react'
import { t as defaultT } from './locales/index.ts'
import type { SidecarLocaleKey } from './locales/index.ts'
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
   * sidecar.command, displayed and edited as one whitespace-joined line;
   * argv splitting/joining is the wiring controller's concern.
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
  /** ui.timeWindowHours (integer ≥ 1) */
  uiTimeWindowHours: number
  /** ui.showDead */
  uiShowDead: boolean
  /** skill.provide */
  skillProvide: boolean
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
  /** Retry hosting after the supervisor tripped `failed`. */
  onDaemonRetry?: () => void
  /** Documentation link target; omitted hides the link. */
  docsUrl?: string
  /** Locale seat override; defaults to the module-local table. */
  t?: SettingsTranslate
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

interface SelectFieldProps {
  label: string
  hint: string
  value: string
  options: readonly { value: string, label: string }[]
  disabled: boolean
  onCommit: (value: string) => void
}

function SelectField(props: SelectFieldProps): ReactNode {
  const id = useId()
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <select
        id={id}
        className={css['select']}
        value={props.value}
        disabled={props.disabled}
        onChange={(event) => { props.onCommit(event.target.value) }}
      >
        {props.options.map(option => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

interface ToggleFieldProps {
  label: string
  hint: string
  checked: boolean
  disabled: boolean
  onCommit: (checked: boolean) => void
}

function ToggleField(props: ToggleFieldProps): ReactNode {
  return (
    <div className={css['field']}>
      <label className={css['toggleRow']}>
        <input
          type="checkbox"
          checked={props.checked}
          disabled={props.disabled}
          onChange={(event) => { props.onCommit(event.target.checked) }}
        />
        <span className={css['label']}>{props.label}</span>
      </label>
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

interface TextFieldProps {
  label: string
  hint: string
  value: string
  placeholder?: string
  disabled: boolean
  onCommit: (value: string) => void
}

/** Text field with a local draft so typing survives a non-echoing beat. */
function TextField(props: TextFieldProps): ReactNode {
  const id = useId()
  const [draft, setDraft] = useState(props.value)
  useEffect(() => { setDraft(props.value) }, [props.value])
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <input
        id={id}
        className={css['input']}
        type="text"
        value={draft}
        placeholder={props.placeholder ?? ''}
        disabled={props.disabled}
        onChange={(event) => {
          setDraft(event.target.value)
          props.onCommit(event.target.value)
        }}
      />
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

interface NumberFieldProps {
  label: string
  hint: string
  /** Copy shown while the draft is not an acceptable integer. */
  invalidHint: string
  /** Schema lower bound (host Config schemastery `.min()`). */
  min: number
  value: number
  disabled: boolean
  onCommit: (value: number) => void
}

/**
 * Integer field with a local draft: invalid intermediate text (empty,
 * non-numeric, below the schema minimum) shows the invalid hint and commits
 * nothing, so the staged value can never leave the schema's domain.
 */
function NumberField(props: NumberFieldProps): ReactNode {
  const id = useId()
  const [draft, setDraft] = useState(String(props.value))
  const [invalid, setInvalid] = useState(false)
  useEffect(() => {
    setDraft(String(props.value))
    setInvalid(false)
  }, [props.value])
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <input
        id={id}
        className={invalid ? `${css['input']} ${css['inputInvalid']}` : css['input']}
        type="text"
        inputMode="numeric"
        value={draft}
        disabled={props.disabled}
        {...invalid ? { 'aria-invalid': true } : {}}
        onChange={(event) => {
          const text = event.target.value
          setDraft(text)
          const parsed = Number(text)
          const acceptable = text.trim() !== '' && Number.isInteger(parsed) && parsed >= props.min
          setInvalid(!acceptable)
          if (acceptable && parsed !== props.value) props.onCommit(parsed)
        }}
      />
      {invalid
        ? <p className={css['invalidHint']} role="alert">{props.invalidHint}</p>
        : <p className={css['hint']}>{props.hint}</p>}
    </div>
  )
}

/**
 * Render the Agent Sidecar settings card.
 * @param props - staged values, form state, and the wiring callbacks.
 * @returns the card.
 */
export function SettingsCard(props: SettingsCardProps): ReactNode {
  const [open, setOpen] = useState(false)
  const t = props.t ?? defaultT
  const { values } = props
  const disabled = !props.writable || props.saving
  const title = t('settings.cardTitle')

  const daemonNote = props.daemon?.state === 'defer'
    ? t('settings.daemonDeferNote')
    : props.daemon?.state === 'failed'
      ? t('settings.daemonFailedNote')
      : undefined

  return (
    <li className={`${css['card']} ${open ? css['cardOpen'] : ''}`}>
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
        {props.dirty ? <span className={css['pending']}>{t('settings.unsaved')}</span> : null}
        <span className={`${css['chevron']} ${open ? css['chevronOpen'] : ''}`} aria-hidden />
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
                          <button
                            type="button"
                            className={css['retry']}
                            onClick={props.onDaemonRetry}
                          >
                            {t('settings.daemonRetry')}
                          </button>
                        )
                        : null}
                    </div>
                    {daemonNote !== undefined
                      ? <p className={css['note']}>{daemonNote}</p>
                      : null}
                  </div>
                )
                : null}
              <SelectField
                label={t('settings.daemonPolicyLabel')}
                hint={t('settings.daemonPolicyHint')}
                value={values.daemonPolicy}
                disabled={disabled}
                options={[
                  { value: 'adopt-or-host', label: t('settings.daemonPolicyAdoptOrHost') },
                  { value: 'adopt-only', label: t('settings.daemonPolicyAdoptOnly') },
                  { value: 'off', label: t('settings.daemonPolicyOff') },
                ]}
                onCommit={(value) => {
                  props.onChange('daemonPolicy', value as SettingsCardValues['daemonPolicy'])
                }}
              />
              <NumberField
                label={t('settings.daemonBackoffLimitLabel')}
                hint={t('settings.daemonBackoffLimitHint')}
                invalidHint={t('settings.invalidNumber', { min: 1 })}
                min={1}
                value={values.daemonBackoffLimit}
                disabled={disabled}
                onCommit={(value) => { props.onChange('daemonBackoffLimit', value) }}
              />
            </Section>

            <Section title={t('settings.sectionSidecar')}>
              <TextField
                label={t('settings.sidecarCommandLabel')}
                hint={t('settings.sidecarCommandHint')}
                value={values.sidecarCommand}
                placeholder="agent-sidecar"
                disabled={disabled}
                onCommit={(value) => { props.onChange('sidecarCommand', value) }}
              />
              <TextField
                label={t('settings.sidecarRuntimeDirLabel')}
                hint={t('settings.sidecarRuntimeDirHint')}
                value={values.sidecarRuntimeDir}
                placeholder="~/.agent_sidecar"
                disabled={disabled}
                onCommit={(value) => { props.onChange('sidecarRuntimeDir', value) }}
              />
            </Section>

            <Section title={t('settings.sectionStream')}>
              <NumberField
                label={t('settings.streamActiveMsLabel')}
                hint={t('settings.streamActiveMsHint')}
                invalidHint={t('settings.invalidNumber', { min: 100 })}
                min={100}
                value={values.streamReconcileActiveMs}
                disabled={disabled}
                onCommit={(value) => { props.onChange('streamReconcileActiveMs', value) }}
              />
              <NumberField
                label={t('settings.streamIdleMsLabel')}
                hint={t('settings.streamIdleMsHint')}
                invalidHint={t('settings.invalidNumber', { min: 100 })}
                min={100}
                value={values.streamReconcileIdleMs}
                disabled={disabled}
                onCommit={(value) => { props.onChange('streamReconcileIdleMs', value) }}
              />
            </Section>

            <Section title={t('settings.sectionInject')}>
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
              <ToggleField
                label={t('settings.analysisEnabledLabel')}
                hint={t('settings.analysisEnabledHint')}
                checked={values.analysisEnabled}
                disabled={disabled}
                onCommit={(checked) => { props.onChange('analysisEnabled', checked) }}
              />
            </Section>

            <Section title={t('settings.sectionUi')}>
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

            <Section title={t('settings.sectionSkill')}>
              <ToggleField
                label={t('settings.skillProvideLabel')}
                hint={t('settings.skillProvideHint')}
                checked={values.skillProvide}
                disabled={disabled}
                onCommit={(checked) => { props.onChange('skillProvide', checked) }}
              />
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
              <button
                type="button"
                className={css['discard']}
                disabled={!props.dirty || props.saving}
                onClick={props.onDiscard}
              >
                {t('settings.discard')}
              </button>
              <button
                type="button"
                className={css['save']}
                disabled={!props.dirty || props.saving || !props.writable}
                onClick={props.onSave}
              >
                {t(props.saving ? 'settings.saving' : 'settings.save')}
              </button>
            </div>
          </div>
        )
        : null}
    </li>
  )
}
