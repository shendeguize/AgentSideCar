import { Button, Input } from '@deepseek-ai/dsh-client-ui-primitives'
import { useState } from 'react'
import type { ReactElement } from 'react'
import { t } from '../locales/index.ts'
import type { AnalysisExchange, AnalysisGlueState } from '../analysis-glue.ts'
import { surfaceProps } from '../theme/parts.ts'
import css from './analysis.module.css'

export interface AnalysisPanelProps {
  /** Live `analysis.enabled` bit (settings scope); false disables intents. */
  enabled: boolean
  state: AnalysisGlueState
  onStart: () => void
  onFollowup: (question: string) => void
  onStop: () => void
  onClose: () => void
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

function Exchange(props: { exchange: AnalysisExchange }): ReactElement {
  const { exchange } = props
  return (
    <div className={css['exchange']} data-testid="agent-sidecar-analysis-exchange">
      <span className={css['exchangeLabel']}>
        {exchange.question === null ? t('analysis.exchangeInitial') : t('analysis.followupLabel')}
      </span>
      {exchange.question !== null && <div className={css['question']}>{exchange.question}</div>}
      <pre className={css['summary']}>
        {exchange.summary === '' ? t('analysis.emptySummary') : exchange.summary}
      </pre>
      {exchange.truncated && (
        <span className={css['truncated']}>{t('analysis.truncatedNotice')}</span>
      )}
    </div>
  )
}

/** The analysis panel. Pure render over props (state machine lives in glue). */
export function AnalysisPanel(props: AnalysisPanelProps): ReactElement {
  const { enabled, state } = props
  const [question, setQuestion] = useState('')

  const conversationLive = state.phase === 'ready' || state.phase === 'answering'
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
        {conversationLive && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={!enabled}
            onClick={props.onStop}
            data-testid="agent-sidecar-analysis-stop"
          >
            {t('analysis.stop')}
          </Button>
        )}
        <Button type="button" size="sm" variant="ghost" onClick={props.onClose}>
          {t('analysis.close')}
        </Button>
      </div>

      {!enabled && (
        <div className={css['noteCard']} data-testid="agent-sidecar-analysis-disabled">
          {t('analysis.disabledNote')}
        </div>
      )}

      {state.exchanges.map((exchange, index) => (
        <Exchange key={index} exchange={exchange} />
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
