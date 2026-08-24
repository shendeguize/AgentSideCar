/**
 * Inject panel (design §5.1 view 3): the two-phase confirmation UI for
 * message injection. Fully controlled and presentational — the component
 * does NO data fetching; the integration layer supplies `onPrepare` /
 * `onExecute` callbacks that speak to `POST <prefix>/action` and resolve
 * with either the wire success shape or the data layer's normalized
 * ApiError (matched structurally, never imported).
 *
 * Three zones, driven by the pure reducer in ./logic.ts:
 *
 * 1. editor  — message textarea + live UTF-8 byte counter, queue/steer mode
 *    radios, target row, cursor-cli process-list warning (§4.d/S7) and the
 *    audit-fingerprint note (§5.3);
 * 2. confirm — the prepare plan (live target-status snapshot + message
 *    digest), the 60s confirmToken countdown, and the deliberately
 *    restrained danger button;
 * 3. result  — delivered / failed (re-prepare offered) / unknown (terminal:
 *    the reducer itself refuses a reset, so no retry affordance can exist —
 *    S6 — and the copy points at the session to verify).
 *
 * `capability.inject === false` disables the whole panel with the
 * "enable injection in Settings" note.
 */

import { useEffect, useId, useReducer, useState } from 'react'
import type { ReactNode } from 'react'
import { t as defaultT } from '../locales/index.ts'
import type { SidecarLocaleKey } from '../locales/index.ts'
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
  InjectPlanView,
  InjectResultView,
  PanelEvent,
  PanelExecuteRequest,
  PanelPrepareRequest,
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

// ---------------------------------------------------------------------------
// Zone building blocks.
// ---------------------------------------------------------------------------

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
      <p className={css['auditNote']}>{props.t('inject.auditNote')}</p>
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
          <span className={css['agentTag']}>{plan.target.agent}</span>
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
        <span className={css['planValue']}>{t(MODE_COPY[plan.mode].label)}</span>
      </div>
      <div className={css['planPreview']}>
        <span className={css['planKey']}>
          {t('inject.planPreviewLabel', { bytes: plan.messagePreview.bytes })}
        </span>
        <pre className={css['preview']}>{plan.messagePreview.head}</pre>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Panel.
// ---------------------------------------------------------------------------

/**
 * Render the two-phase inject panel.
 * @param props - capability, target, and the integration callbacks.
 * @returns the panel.
 */
export function InjectPanel(props: InjectPanelProps): ReactNode {
  const t = props.t ?? defaultT
  const now = props.nowMs ?? Date.now
  const [state, dispatch] = useReducer(reducePanel, undefined, initialPanelState)
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState<InjectMode>(props.defaultMode)
  const [clock, setClock] = useState(() => now())
  const textareaId = useId()
  const modeGroup = useId()

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

  const validation = validateMessage(draft)
  const gate = deriveEditorGate({
    injectEnabled: props.capability.inject,
    hasTarget: props.target !== null,
    phase: state.phase,
    validation,
  })

  const handlePrepare = async (): Promise<void> => {
    const target = props.target
    if (target === null || !gate.canPrepare) return
    const message = draft
    dispatch({ type: 'PREPARE_START', message, mode })
    let event: PanelEvent
    try {
      const response = await props.onPrepare({
        target: { agent: target.agent, sessionId: target.sessionId },
        mode,
        message,
      })
      event = classifyPrepareResponse(response)
    } catch {
      // Contract says resolve errors as values; a throw is still mapped to
      // a retryable transport notice rather than a crashed panel.
      event = { type: 'PREPARE_ERROR', code: 'unexpected_error' }
    }
    dispatch(event)
  }

  const handleExecute = async (): Promise<void> => {
    if (state.phase !== 'confirm') return
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

  const closeButton = props.onClose !== undefined
    ? (
      <button type="button" className={css['btn']} onClick={props.onClose}>
        {t('inject.close')}
      </button>
    )
    : null

  // ── whole-panel gate: injection capability off ─────────────────────────
  if (!props.capability.inject) {
    return (
      <section className={css['panel']} aria-label={t('inject.title')} onKeyDown={handleKeyDown}>
        <header className={css['header']}>
          <h3 className={css['title']}>{t('inject.title')}</h3>
        </header>
        <div className={css['body']}>
          <p className={css['capabilityOff']} role="status">{t('inject.capabilityOff')}</p>
          {closeButton !== null
            ? <div className={css['footer']}>{closeButton}</div>
            : null}
        </div>
      </section>
    )
  }

  // ── zone 3: result ──────────────────────────────────────────────────────
  if (state.phase === 'result') {
    const { result } = state
    const actions = resultActions(result.outcome)
    const toneClass = result.outcome === 'delivered'
      ? css['resultOk']
      : result.outcome === 'failed'
        ? css['resultFail']
        : css['resultUnknown']
    return (
      <section className={css['panel']} aria-label={t('inject.title')} onKeyDown={handleKeyDown}>
        <header className={css['header']}>
          <h3 className={css['title']}>{t('inject.title')}</h3>
        </header>
        <div className={css['body']}>
          <p className={toneClass} role="status">{t(RESULT_COPY[result.outcome])}</p>
          {result.outcome === 'failed' && result.errorCode !== undefined
            ? (
              <p className={css['resultDetail']}>
                {renderCopy(t, errorCopy(result.errorCode))}
              </p>
            )
            : null}
          {result.replayed === true
            ? <p className={css['resultDetail']}>{t('inject.resultReplayed')}</p>
            : null}
          <div className={css['footer']}>
            {closeButton}
            {isDeliveredResult(result) && props.onObserve !== undefined
              ? (
                <button
                  type="button"
                  className={css['btnPrimary']}
                  onClick={props.onObserve}
                  data-testid="agent-sidecar-inject-observe"
                >
                  {t('inject.observeListen')}
                </button>
              )
              : null}
            {actions.canReprepare
              ? (
                <button
                  type="button"
                  className={css['btnPrimary']}
                  onClick={() => { dispatch({ type: 'RESET' }) }}
                >
                  {t('inject.reprepare')}
                </button>
              )
              : null}
            {result.outcome === 'delivered' && props.onClose === undefined
              ? (
                <button
                  type="button"
                  className={css['btnPrimary']}
                  onClick={() => { dispatch({ type: 'RESET' }) }}
                >
                  {t('inject.done')}
                </button>
              )
              : null}
          </div>
        </div>
      </section>
    )
  }

  // ── zone 2: confirm / executing ─────────────────────────────────────────
  if (state.phase === 'confirm' || state.phase === 'executing') {
    const executing = state.phase === 'executing'
    const countdown = state.phase === 'confirm'
      ? tokenCountdown(state.expiresAt, clock)
      : null
    return (
      <section
        className={css['panel']}
        aria-label={t('inject.confirmTitle')}
        onKeyDown={handleKeyDown}
      >
        <header className={css['header']}>
          <h3 className={css['title']}>{t('inject.confirmTitle')}</h3>
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
            <button
              type="button"
              className={css['btn']}
              disabled={executing}
              onClick={() => { dispatch({ type: 'CANCEL' }) }}
            >
              {t('inject.cancel')}
            </button>
            <button
              type="button"
              className={css['btnDanger']}
              disabled={executing}
              onClick={() => { void handleExecute() }}
            >
              {t(executing ? 'inject.executing' : 'inject.confirmExecute')}
            </button>
          </div>
        </div>
      </section>
    )
  }

  // ── zone 1: editor (idle / preparing) ───────────────────────────────────
  const preparing = state.phase === 'preparing'
  const canEdit = props.target !== null && !preparing
  const usage = byteUsage(validation.bytes)
  const notice = state.phase === 'idle' ? state.notice : null
  const showInvalid = !validation.ok && validation.code !== 'empty'

  return (
    <section className={css['panel']} aria-label={t('inject.title')} onKeyDown={handleKeyDown}>
      <header className={css['header']}>
        <h3 className={css['title']}>{t('inject.title')}</h3>
      </header>
      <div className={css['body']}>
        {notice !== null
          ? (
            <p
              className={notice.kind === 'token_expired' ? css['noticeWarn'] : css['noticeError']}
              role="alert"
            >
              {renderCopy(t, noticeCopy(notice))}
              {notice.kind === 'prepare_rejected' && notice.detail !== undefined
                ? <span className={css['noticeDetail']}> {notice.detail}</span>
                : null}
            </p>
          )
          : null}

        <div className={css['targetRow']}>
          <span className={css['planKey']}>{t('inject.targetLabel')}</span>
          {props.target !== null
            ? (
              <span className={css['planValue']}>
                <span className={css['agentTag']}>{props.target.agent}</span>
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
            placeholder={t('inject.messagePlaceholder')}
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

        <Warnings t={t} agent={props.target?.agent ?? null} />

        <div className={css['footer']}>
          {closeButton}
          <button
            type="button"
            className={css['btnPrimary']}
            disabled={!gate.canPrepare}
            onClick={() => { void handlePrepare() }}
          >
            {t(preparing ? 'inject.preparing' : 'inject.prepare')}
          </button>
        </div>
      </div>
    </section>
  )
}
