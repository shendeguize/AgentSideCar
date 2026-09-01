import { Button, Pill } from '@deepseek-ai/dsh-client-ui-primitives'
import { useEffect, useId, useReducer, useState } from 'react'
import type { ReactNode } from 'react'
import { injectBlockReason } from '../api.ts'
import type {
  InjectBlockReason,
  InjectEligibility,
} from '../api.ts'
import { t as defaultT } from '../locales/index.ts'
import type { SidecarLocaleKey } from '../locales/index.ts'
import { surfaceProps } from '../theme/parts.ts'
import css from './inject.module.css'
import {
  byteUsage,
  classifyExecuteResponse,
  classifyPanelKey,
  classifyPrepareResponse,
  deriveEditorGate,
  errorCopy,
  isDeliveredResult,
  initialPanelState,
  messageInvalidCopy,
  MODE_COPY,
  noticeCopy,
  reducePanel,
  RESULT_COPY,
  resultActions,
  showsProcessListWarning,
  tokenCountdown,
  validateMessage,
} from './logic.ts'
import type {
  ApiErrorLike,
  CopyRef,
  InjectMode,
  InjectOutcome,
  InjectPlanView,
  InjectResultView,
  PanelEvent,
  PanelExecuteRequest,
  PanelPrepareRequest,
  PanelState,
  PrepareSuccess,
} from './logic.ts'

/** Translate seat the panel consumes (module-local t by default). */
export type InjectTranslate = (
  key: SidecarLocaleKey,
  params?: Record<string, unknown>,
) => string

/** The session the panel injects into, as picked on the board/detail view. */
export interface InjectPanelTarget {
  agent: string
  sessionId: string
  title?: string
}

export interface InjectPanelProps {
  /** Server capability bits (from the state snapshot). */
  capability: { inject: boolean }
  /** Host-derived target verdict; missing/unknown values fail closed. */
  eligibility?: InjectEligibility | null
  /** Injection target; null renders the editor disabled with a hint. */
  target: InjectPanelTarget | null
  /** Mode preselected on mount (the inject.default-mode setting). */
  defaultMode: InjectMode
  /** Phase one: POST inject.prepare (resolve errors as values, don't throw). */
  onPrepare(req: PanelPrepareRequest): Promise<PrepareSuccess | ApiErrorLike>
  /** Phase two: POST inject.execute (resolve errors as values, don't throw). */
  onExecute(req: PanelExecuteRequest): Promise<InjectResultView | ApiErrorLike>
  /** Close/dismiss the panel (the hosting modal owns the lifecycle). */
  onClose?(): void
  /**
   * UX-05 observation loop: when given, the delivered result page offers a
   * 「开启监听观察反应」 button (the host typically enables listen mode on
   * the detail timeline and closes the panel).
   */
  onObserve?(): void
  /** Clock injection for the token countdown; defaults to Date.now. */
  nowMs?: () => number
  /** Locale seat override; defaults to the module-local table. */
  t?: InjectTranslate
}

/** Countdown re-render cadence while a confirm token is live. */
const TICK_MS = 500

function renderCopy(t: InjectTranslate, copy: CopyRef): string {
  return t(copy.key, copy.params)
}

/**
 * Client-visible eligibility vocabulary. The shared inject logic predates
 * these host verdicts, so this local overlay keeps every known reason
 * localized without reflecting a raw machine code.
 */
export const ERROR_COPY: Readonly<Record<string, SidecarLocaleKey>> = {
  inject_disabled: 'inject.errInjectDisabled',
  unsupported_agent: 'inject.errUnsupportedAgent',
  working_session: 'inject.errWorkingSession',
  dead_session: 'inject.errTargetDead',
  target_dead: 'inject.errTargetDead',
  child_session: 'inject.errChildSession',
  remote_session: 'inject.errRemoteSession',
  invalid_session: 'inject.errInvalidSession',
}

/** Known gateway copy plus a code-free fallback for unexpected failures. */
export function injectErrorCopy(code: string): CopyRef {
  const eligibilityKey = ERROR_COPY[code]
  if (eligibilityKey !== undefined) return { key: eligibilityKey }
  const copy = errorCopy(code)
  return copy.key === 'inject.errGeneric' ? { key: 'inject.errUnknown' } : copy
}

type EligibilityAwarePanelEvent =
  | PanelEvent
  | { type: 'ELIGIBILITY_BLOCKED' }

export function reduceEligibilityAwarePanel(
  state: PanelState,
  event: EligibilityAwarePanelEvent,
): PanelState {
  if (event.type !== 'ELIGIBILITY_BLOCKED') return reducePanel(state, event)
  // Void any prepared token/client request when the live target closes.
  // An already executing/terminal delivery remains observable and cannot be
  // honestly cancelled or made retryable by a client-side status change.
  return state.phase === 'preparing' || state.phase === 'confirm'
    ? initialPanelState()
    : state
}

function isKimiAgent(agent: string | null | undefined): boolean {
  return agent?.trim().toLowerCase() === 'kimi'
}

/** Kimi's protected-resume transport has no mid-turn steering path. */
export function effectiveInjectMode(agent: string, selectedMode: InjectMode): InjectMode {
  return isKimiAgent(agent) ? 'queue' : selectedMode
}

/** Build the exact prepare request after applying agent-specific mode safety. */
export function createPanelPrepareRequest(
  target: InjectPanelTarget,
  message: string,
  selectedMode: InjectMode,
): PanelPrepareRequest {
  return {
    target: { agent: target.agent, sessionId: target.sessionId },
    mode: effectiveInjectMode(target.agent, selectedMode),
    message,
  }
}

/** Kimi ACP now exposes a durable completion receipt through Sidecar. */
export function displayInjectOutcome(agent: string, outcome: InjectOutcome): InjectOutcome {
  return outcome
}

/** Select the honest result headline for the target transport. */
export function resultCopyKey(agent: string, outcome: InjectOutcome): SidecarLocaleKey {
  const displayOutcome = displayInjectOutcome(agent, outcome)
  if (isKimiAgent(agent)) {
    if (displayOutcome === 'unknown') return 'inject.kimiResultUnknown'
    if (displayOutcome === 'failed') return 'inject.kimiResultFailed'
  }
  return RESULT_COPY[displayOutcome]
}

interface WarningsProps {
  t: InjectTranslate
  agent: string | null
}

/** cursor-cli process-list warning (gated) + the always-on audit note. */
function Warnings(props: WarningsProps): ReactNode {
  return (
    <>
      {props.agent !== null && showsProcessListWarning(props.agent)
        ? <p className={css['warnBar']} role="alert">{props.t('inject.argvWarning')}</p>
        : null}
      <p className={css['auditNote']}>
        {props.t(isKimiAgent(props.agent) ? 'inject.kimiAuditNote' : 'inject.auditNote')}
      </p>
    </>
  )
}

interface PlanBoxProps {
  t: InjectTranslate
  plan: InjectPlanView
}

/** Confirm-phase plan: live target snapshot + message digest. */
function PlanBox(props: PlanBoxProps): ReactNode {
  const { t, plan } = props
  const status = plan.targetStatus
  const targetName = status.title !== undefined && status.title !== ''
    ? status.title
    : plan.target.sessionId
  return (
    <div className={css['planBox']}>
      <div className={css['planRow']}>
        <span className={css['planKey']}>{t('inject.planTargetLabel')}</span>
        <span className={css['planValue']}>
          <Pill className={css['agentTag']}>{plan.target.agent}</Pill>
          <span className={css['planTitle']} title={plan.target.sessionId}>{targetName}</span>
        </span>
      </div>
      <div className={css['planRow']}>
        <span className={css['planKey']}>
          {t('inject.planStatus', { status: status.status })}
        </span>
      </div>
      <p className={css['observedNote']}>{t('inject.statusObservedNote')}</p>
      <div className={css['planRow']}>
        <span className={css['planKey']}>{t('inject.planModeLabel')}</span>
        <span className={css['planValue']}>
          {t(isKimiAgent(plan.target.agent)
            ? 'inject.kimiModeLabel'
            : MODE_COPY[plan.mode].label)}
        </span>
      </div>
      {isKimiAgent(plan.target.agent)
        ? <p className={css['observedNote']}>{t('inject.kimiModeHint')}</p>
        : null}
      <div className={css['planPreview']}>
        <span className={css['planKey']}>
          {t('inject.planPreviewLabel', { bytes: plan.messagePreview.bytes })}
        </span>
        <pre className={css['preview']}>{plan.messagePreview.head}</pre>
      </div>
    </div>
  )
}

export function InjectPanel(props: InjectPanelProps): ReactNode {
  const t = props.t ?? defaultT
  const now = props.nowMs ?? Date.now
  const isKimi = isKimiAgent(props.target?.agent)
  const [state, dispatch] = useReducer(
    reduceEligibilityAwarePanel,
    undefined,
    initialPanelState,
  )
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState<InjectMode>(() =>
    props.target === null
      ? props.defaultMode
      : effectiveInjectMode(props.target.agent, props.defaultMode))
  const [clock, setClock] = useState(() => now())
  const textareaId = useId()
  const modeGroup = useId()
  const blockedReasonId = useId()
  const panelTitleKey: SidecarLocaleKey = isKimi ? 'inject.kimiTitle' : 'inject.title'
  const blockReason: InjectBlockReason | null =
    props.capability.inject
      ? props.target === null
        ? null
        : injectBlockReason(true, props.eligibility)
      : 'inject_disabled'

  // Token countdown: refresh the display clock and let the reducer decide
  // expiry, only while a confirm token is live.
  const inConfirm = state.phase === 'confirm'
  useEffect(() => {
    if (!inConfirm) return
    setClock(now())
    const id = setInterval(() => {
      const nowValue = now()
      setClock(nowValue)
      dispatch({ type: 'TICK', nowMs: nowValue })
    }, TICK_MS)
    return () => { clearInterval(id) }
    // `now` is a stable prop (or Date.now); phase entry is the real trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inConfirm])

  useEffect(() => {
    if (blockReason !== null) dispatch({ type: 'ELIGIBILITY_BLOCKED' })
  }, [blockReason])

  const validation = validateMessage(draft)
  const gate = deriveEditorGate({
    injectEnabled: props.capability.inject && blockReason === null,
    hasTarget: props.target !== null,
    phase: state.phase,
    validation,
  })

  const handlePrepare = async (): Promise<void> => {
    const target = props.target
    if (target === null || blockReason !== null || !gate.canPrepare) return
    const message = draft
    const request = createPanelPrepareRequest(target, message, mode)
    dispatch({ type: 'PREPARE_START', message, mode: request.mode })
    let event: PanelEvent
    try {
      const response = await props.onPrepare(request)
      event = classifyPrepareResponse(response)
    } catch {
      // Contract says resolve errors as values; a throw is still mapped to
      // a retryable transport notice rather than a crashed panel.
      event = { type: 'PREPARE_ERROR', code: 'unexpected_error' }
    }
    dispatch(event)
  }

  const handleExecute = async (): Promise<void> => {
    if (state.phase !== 'confirm' || blockReason !== null) return
    const { requestId, confirmToken, message } = state
    dispatch({ type: 'EXECUTE_START' })
    let event: PanelEvent
    try {
      const response = await props.onExecute({ requestId, confirmToken, message })
      event = classifyExecuteResponse(response)
    } catch {
      // The execute may already have been dispatched server-side: honest
      // verdict is unknown — terminal, no retry (S6).
      event = {
        type: 'EXECUTE_RESULT',
        result: { outcome: 'unknown', errorCode: 'unexpected_error' },
      }
    }
    dispatch(event)
  }

  // Keyboard affordances (UX-10): Esc closes in every zone; Cmd/Ctrl+Enter
  // triggers PREPARE only (classifyPanelKey refuses everything else, so no
  // keyboard path can shortcut the explicit confirm click).
  const handleKeyDown = (event: {
    key: string
    metaKey: boolean
    ctrlKey: boolean
    stopPropagation(): void
    preventDefault(): void
  }): void => {
    const intent = classifyPanelKey({
      key: event.key,
      metaKey: event.metaKey,
      ctrlKey: event.ctrlKey,
      phase: state.phase,
      canPrepare: gate.canPrepare,
    })
    if (intent === 'close' && props.onClose !== undefined) {
      event.stopPropagation()
      props.onClose()
    } else if (intent === 'prepare') {
      event.preventDefault()
      void handlePrepare()
    }
  }

  const panelSurface = surfaceProps('inject-panel', css['panel'])
  const closeButton = props.onClose !== undefined
    ? (
      <Button type="button" size="sm" variant="outline" onClick={props.onClose}>
        {t('inject.close')}
      </Button>
    )
    : null

  if (blockReason !== null) {
    const blockedCopy = renderCopy(t, injectErrorCopy(blockReason))
    return (
      <section
        {...panelSurface}
        aria-label={t(panelTitleKey)}
        aria-describedby={blockedReasonId}
        onKeyDown={handleKeyDown}
      >
        <header className={css['header']}>
          <h3 className={css['title']}>{t(panelTitleKey)}</h3>
        </header>
        <div className={css['body']}>
          <p
            id={blockedReasonId}
            className={css['capabilityOff']}
            role="status"
            data-testid="agent-sidecar-inject-blocked-reason"
          >
            {isKimi && blockReason === 'working_session'
              ? t('inject.kimiErrWorkingSession')
              : blockedCopy}
          </p>
          {closeButton !== null
            ? <div className={css['footer']}>{closeButton}</div>
            : null}
        </div>
      </section>
    )
  }

  if (state.phase === 'result') {
    const { result } = state
    const resultAgent = state.plan?.target.agent ?? ''
    const isKimiResult = isKimiAgent(resultAgent)
    const displayOutcome = displayInjectOutcome(resultAgent, result.outcome)
    const actions = resultActions(displayOutcome)
    const toneClass = displayOutcome === 'delivered'
      ? css['resultOk']
      : displayOutcome === 'failed'
        ? css['resultFail']
        : css['resultUnknown']
    const resultCopy = resultCopyKey(resultAgent, result.outcome)
    return (
      <section
        {...panelSurface}
        aria-label={t(isKimiResult ? 'inject.kimiTitle' : 'inject.title')}
        onKeyDown={handleKeyDown}
      >
        <header className={css['header']}>
          <h3 className={css['title']}>
            {t(isKimiResult ? 'inject.kimiTitle' : 'inject.title')}
          </h3>
        </header>
        <div className={css['body']}>
          <p className={toneClass} role="status">{t(resultCopy)}</p>
          {displayOutcome === 'failed' && result.errorCode !== undefined
            ? (
              <p className={css['resultDetail']}>
                {renderCopy(t, injectErrorCopy(result.errorCode))}
              </p>
            )
            : null}
          {result.replayed === true
            ? (
              <p className={css['resultDetail']}>
                {t(isKimiResult ? 'inject.kimiResultReplayed' : 'inject.resultReplayed')}
              </p>
            )
            : null}
          <div className={css['footer']}>
            {closeButton}
            {!isKimiResult && isDeliveredResult(result) && props.onObserve !== undefined
              ? (
                <Button
                  type="button"
                  size="sm"
                  variant="primary"
                  onClick={props.onObserve}
                  data-testid="agent-sidecar-inject-observe"
                >
                  {t('inject.observeListen')}
                </Button>
              )
              : null}
            {actions.canReprepare
              ? (
                <Button
                  type="button"
                  size="sm"
                  variant="primary"
                  onClick={() => { dispatch({ type: 'RESET' }) }}
                >
                  {t('inject.reprepare')}
                </Button>
              )
              : null}
            {!isKimiResult && result.outcome === 'delivered' && props.onClose === undefined
              ? (
                <Button
                  type="button"
                  size="sm"
                  variant="primary"
                  onClick={() => { dispatch({ type: 'RESET' }) }}
                >
                  {t('inject.done')}
                </Button>
              )
              : null}
          </div>
        </div>
      </section>
    )
  }

  if (state.phase === 'confirm' || state.phase === 'executing') {
    const executing = state.phase === 'executing'
    const isKimiPlan = isKimiAgent(state.plan.target.agent)
    const countdown = state.phase === 'confirm'
      ? tokenCountdown(state.expiresAt, clock)
      : null
    return (
      <section
        {...panelSurface}
        aria-label={t(isKimiPlan ? 'inject.kimiConfirmTitle' : 'inject.confirmTitle')}
        onKeyDown={handleKeyDown}
      >
        <header className={css['header']}>
          <h3 className={css['title']}>
            {t(isKimiPlan ? 'inject.kimiConfirmTitle' : 'inject.confirmTitle')}
          </h3>
        </header>
        <div className={css['body']}>
          <PlanBox t={t} plan={state.plan} />
          <Warnings t={t} agent={state.plan.target.agent} />
          {countdown !== null
            ? (
              <p className={css['countdown']} role="timer">
                {t('inject.countdown', { seconds: countdown.seconds })}
              </p>
            )
            : null}
          <div className={css['footer']}>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={executing}
              onClick={() => { dispatch({ type: 'CANCEL' }) }}
            >
              {t('inject.cancel')}
            </Button>
            <button
              type="button"
              className={css['btnDanger']}
              disabled={executing}
              onClick={() => { void handleExecute() }}
            >
              {t(isKimiPlan
                ? executing
                  ? 'inject.kimiExecuting'
                  : 'inject.kimiConfirmExecute'
                : executing
                  ? 'inject.executing'
                  : 'inject.confirmExecute')}
            </button>
          </div>
        </div>
      </section>
    )
  }

  const preparing = state.phase === 'preparing'
  const canEdit = props.target !== null && !preparing
  const usage = byteUsage(validation.bytes)
  const notice = state.phase === 'idle' ? state.notice : null
  const showInvalid = !validation.ok && validation.code !== 'empty'

  return (
    <section {...panelSurface} aria-label={t(panelTitleKey)} onKeyDown={handleKeyDown}>
      <header className={css['header']}>
        <h3 className={css['title']}>{t(panelTitleKey)}</h3>
      </header>
      <div className={css['body']}>
        {notice !== null
          ? (
            <p
              className={notice.kind === 'token_expired' ? css['noticeWarn'] : css['noticeError']}
              role="alert"
            >
              {renderCopy(
                t,
                notice.kind === 'token_expired'
                  ? noticeCopy(notice)
                  : injectErrorCopy(notice.code),
              )}
            </p>
          )
          : null}

        <div className={css['targetRow']}>
          <span className={css['planKey']}>{t('inject.targetLabel')}</span>
          {props.target !== null
            ? (
              <span className={css['planValue']}>
                <Pill className={css['agentTag']}>{props.target.agent}</Pill>
                <span className={css['planTitle']} title={props.target.sessionId}>
                  {props.target.title !== undefined && props.target.title !== ''
                    ? props.target.title
                    : props.target.sessionId}
                </span>
              </span>
            )
            : <span className={css['noTarget']}>{t('inject.noTarget')}</span>}
        </div>

        <div className={css['field']}>
          <label className={css['label']} htmlFor={textareaId}>
            {t('inject.messageLabel')}
          </label>
          <textarea
            id={textareaId}
            className={css['textarea']}
            value={draft}
            placeholder={t(isKimi
              ? 'inject.kimiMessagePlaceholder'
              : 'inject.messagePlaceholder')}
            disabled={!canEdit}
            rows={5}
            // eslint-disable-next-line jsx-a11y/no-autofocus -- the panel is
            // a modal whose single task starts in this field (UX-10).
            autoFocus
            onChange={(event) => { setDraft(event.target.value) }}
          />
          <div className={css['byteRow']}>
            <div className={css['byteBar']} aria-hidden>
              <div
                className={usage.over
                  ? `${css['byteFill']} ${css['byteFillOver']}`
                  : css['byteFill']}
                style={{ width: `${usage.ratio * 100}%` }}
              />
            </div>
            <span className={usage.over ? css['byteTextOver'] : css['byteText']}>
              {t('inject.byteCount', { bytes: usage.bytes, limit: usage.limit })}
            </span>
          </div>
          {showInvalid && !validation.ok
            ? (
              <p className={css['invalid']} role="alert">
                {renderCopy(t, messageInvalidCopy(validation))}
              </p>
            )
            : null}
        </div>

        {isKimi
          ? (
            <fieldset className={css['modes']}>
              <legend className={css['label']}>{t('inject.kimiActionLabel')}</legend>
              <span className={css['modeText']}>
                <span className={css['modeLabel']}>{t('inject.kimiModeLabel')}</span>
                <span className={css['modeHint']}>{t('inject.kimiModeHint')}</span>
              </span>
            </fieldset>
          )
          : (
            <fieldset className={css['modes']}>
              <legend className={css['label']}>{t('inject.modeLabel')}</legend>
              {(['queue', 'steer'] as const).map(option => (
                <label key={option} className={css['modeOption']}>
                  <input
                    type="radio"
                    name={modeGroup}
                    value={option}
                    checked={mode === option}
                    disabled={!canEdit}
                    onChange={() => { setMode(option) }}
                  />
                  <span className={css['modeText']}>
                    <span className={css['modeLabel']}>{t(MODE_COPY[option].label)}</span>
                    <span className={css['modeHint']}>{t(MODE_COPY[option].hint)}</span>
                  </span>
                </label>
              ))}
            </fieldset>
          )}

        <Warnings t={t} agent={props.target?.agent ?? null} />

        <div className={css['footer']}>
          {closeButton}
          <Button
            type="button"
            size="sm"
            variant="primary"
            disabled={!gate.canPrepare}
            onClick={() => { void handlePrepare() }}
          >
            {t(isKimi
              ? preparing
                ? 'inject.kimiPreparing'
                : 'inject.kimiPrepare'
              : preparing
                ? 'inject.preparing'
                : 'inject.prepare')}
          </Button>
        </div>
      </div>
    </section>
  )
}
