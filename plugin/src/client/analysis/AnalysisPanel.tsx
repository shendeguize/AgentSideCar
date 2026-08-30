import { Button, Input } from '@deepseek-ai/dsh-client-ui-primitives'
import { useState } from 'react'
import type { ReactElement } from 'react'
import { t } from '../locales/index.ts'
import type { AnalysisGlueState, AnalysisMessage } from '../analysis-glue.ts'
import { surfaceProps } from '../theme/parts.ts'
import css from './analysis.module.css'

export interface AnalysisPanelProps {
  /** Live `analysis.enabled` bit (settings scope); false disables intents. */
  enabled: boolean
  state: AnalysisGlueState
  onStart: () => void
  onFollowup: (question: string) => void
  onStop: () => void
  onClose?: () => void
}

/** Terminal failure code → locale key ('' falls back to the generic). */
const ERROR_KEYS: Record<string, Parameters<typeof t>[0]> = {
  analysis_disabled: 'analysis.errDisabled',
  analysis_unavailable: 'analysis.errUnavailable',
  target_not_found: 'analysis.errTargetNotFound',
  too_many_active: 'analysis.errTooManyActive',
  timeout: 'analysis.errTimeout',
  create_failed: 'analysis.errCreateFailed',
  cancelled: 'analysis.errCancelled',
  network_error: 'analysis.errNetwork',
  request_timeout: 'analysis.errNetwork',
}

function errorText(code: string): string {
  const key = ERROR_KEYS[code]
  return key !== undefined ? t(key) : t('analysis.errGeneric', { code })
}

/** Retryable notice code → copy. */
function noticeText(code: string): string {
  if (code === 'timeout') return t('analysis.noticeTimeout')
  if (code === 'cancel_failed') return t('analysis.noticeCancelFailed')
  return t('analysis.noticeNetwork')
}

function messagesFromState(state: AnalysisGlueState): AnalysisMessage[] {
  if (state.messages.length > 0) return state.messages
  return state.exchanges.flatMap((exchange) => [
    ...(exchange.question === null
      ? []
      : [{ role: 'user' as const, content: exchange.question }]),
    { role: 'assistant' as const, content: exchange.summary, truncated: exchange.truncated },
  ])
}

function Message(props: { message: AnalysisMessage; progressStep: number }): ReactElement {
  const { message } = props
  const pendingText = t('analysis.streamingSegment', { n: props.progressStep + 1 })
  return (
    <div
      className={`${css['message']} ${
        message.role === 'user' ? css['userMessage'] : css['assistantMessage']
      }`}
      data-role={message.role}
      data-pending={message.pending || undefined}
      data-testid="agent-sidecar-analysis-message"
      aria-busy={message.pending || undefined}
    >
      <span className={css['messageLabel']}>
        {message.role === 'user' ? t('analysis.userLabel') : t('analysis.assistantLabel')}
      </span>
      <div className={css['messageBody']}>
        {message.pending
          ? pendingText
          : message.content === '' && message.role === 'assistant'
            ? t('analysis.emptySummary')
            : message.content}
      </div>
      {message.truncated && <span className={css['truncated']}>{t('analysis.truncatedNotice')}</span>}
    </div>
  )
}

/** The analysis panel. Pure render over props (state machine lives in glue). */
export function AnalysisPanel(props: AnalysisPanelProps): ReactElement {
  const { enabled, state } = props
  const [question, setQuestion] = useState('')

  const conversationLive = state.phase === 'ready' || state.phase === 'answering'
  const analysisActive =
    state.phase === 'requesting' || state.phase === 'answering' || state.phase === 'ready'
  const showStart = state.phase === 'idle' || state.phase === 'failed' || state.phase === 'stopped'

  const submitFollowup = (): void => {
    const q = question.trim()
    if (q === '' || state.phase !== 'ready') return
    setQuestion('')
    props.onFollowup(q)
  }

  return (
    <section {...surfaceProps('analysis-panel', css['panel'])} data-testid="agent-sidecar-analysis">
      <div className={css['head']}>
        <span className={css['title']}>{t('analysis.title')}</span>
        <span className={css['spacer']} />
        {analysisActive && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={props.onStop}
            data-testid="agent-sidecar-analysis-stop"
          >
            {t('analysis.stop')}
          </Button>
        )}
        {props.onClose !== undefined && (
          <Button type="button" size="sm" variant="ghost" onClick={props.onClose}>
            {t('analysis.close')}
          </Button>
        )}
      </div>

      <div className={css['messages']} data-testid="agent-sidecar-analysis-messages">
        {!enabled && (
          <div className={css['noteCard']} data-testid="agent-sidecar-analysis-disabled">
            {t('analysis.disabledNote')}
          </div>
        )}

        {messagesFromState(state).map((message, index) => (
          <Message
            key={`${message.role}:${index}`}
            message={message}
            progressStep={state.progressStep}
          />
        ))}

        {state.phase === 'failed' && state.errorCode !== null && (
          <div className={css['errorCard']} data-testid="agent-sidecar-analysis-error">
            {errorText(state.errorCode)}
          </div>
        )}
        {state.phase === 'stopped' && (
          <div className={css['noteCard']}>{t('analysis.stopped')}</div>
        )}
        {state.noticeCode !== null && (
          <div className={css['noticeBar']} data-testid="agent-sidecar-analysis-notice">
            {noticeText(state.noticeCode)}
          </div>
        )}

        {enabled && showStart && (
          <>
            {state.phase === 'idle' && (
              <div className={css['mutedLine']}>{t('analysis.idleHint')}</div>
            )}
            <Button
              type="button"
              size="sm"
              variant="primary"
              className={css['startButton']}
              onClick={props.onStart}
              data-testid="agent-sidecar-analysis-start"
            >
              {state.phase === 'idle' ? t('analysis.start') : t('analysis.restart')}
            </Button>
          </>
        )}

        {state.phase === 'requesting' && (
          <div className={css['mutedLine']}>{t('analysis.requesting')}</div>
        )}
        {state.phase === 'answering' && (
          <div className={css['mutedLine']}>{t('analysis.answering')}</div>
        )}
      </div>

      {conversationLive && (
        <div className={css['followupForm']}>
          <Input
            className={css['followupInput']}
            value={question}
            placeholder={t('analysis.followupPlaceholder')}
            disabled={!enabled || state.phase !== 'ready'}
            onChange={(event) => {
              setQuestion(event.currentTarget.value)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') submitFollowup()
            }}
            data-testid="agent-sidecar-analysis-question"
          />
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={!enabled || state.phase !== 'ready' || question.trim() === ''}
            onClick={submitFollowup}
            data-testid="agent-sidecar-analysis-followup"
          >
            {t('analysis.followupSubmit')}
          </Button>
        </div>
      )}

      {(conversationLive || state.exchanges.length > 0) && (
        <div className={css['disclaimer']} data-testid="agent-sidecar-analysis-disclaimer">
          {state.disclaimer ?? t('analysis.disclaimerFallback')}
        </div>
      )}
    </section>
  )
}
