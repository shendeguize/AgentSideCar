/**
 * Batch-archive dialog and the board's archived section.
 *
 * Presentation-only, same contract as Board.tsx: no api/sse imports, every
 * round-trip arrives as a {@link BoardArchiveApi} callback that already
 * speaks board view models (epoch milliseconds, `SessionFocusTarget`).
 *
 * The flow is deliberately two-phase and mirrors the inject gateway:
 * `preview(idleSeconds)` mints a single-use token alongside the candidate
 * list, the operator unchecks whatever should stay, and `apply` hands that
 * token back. A stale dialog therefore cannot replay an old confirmation.
 *
 * @module
 */

import { Button, Modal, Pill } from '@deepseek-ai/dsh-client-ui-primitives'
import { useState } from 'react'
import type { ReactElement } from 'react'
import {
  abbreviateSessionId,
  agentGlyph,
  archiveReasonLabel,
  cardKey,
  countDisposable,
  formatRelativeTime,
  formatTemplate,
  resolveArchiveSeconds,
  shouldOfferDispose,
  sortArchived,
  ARCHIVE_THRESHOLD_SECONDS,
  DEFAULT_ARCHIVE_THRESHOLD,
  type ArchivedCardVM,
  type ArchiveThresholdToken,
  type SessionCardVM,
} from './logic.ts'
import { BOARD_STRINGS } from './strings.ts'
import { surfaceProps } from '../theme/parts.ts'
import styles from './board.module.css'

/** In-memory session identity shared with Board.tsx. */
export interface ArchiveTargetVM {
  agent: string
  sessionId: string
}

/** One `archive.preview` round-trip as the dialog consumes it. */
export interface BoardArchivePreview {
  /** Single-use confirmation token; must be replayed verbatim to `apply`. */
  token: string
  idleSeconds: number
  candidates: SessionCardVM[]
}

/**
 * What one confirmed batch actually did. Archive and dispose are separate
 * round-trips against different owners (the daemon registry and the dsh
 * host), so they are counted separately: a batch that hid ten sessions and
 * failed to end one of them must not report either number as the other.
 */
export interface ArchiveApplyOutcome {
  /** Sessions the daemon hid from the board. */
  archived: number
  /** dsh sessions actually ended; always 0 unless dispose was opted into. */
  disposed: number
  /** dsh sessions the host refused to end; they stay archived. */
  disposeFailed: number
}

/** Owner-supplied archive round-trips (mount.tsx binds these to the api). */
export interface BoardArchiveApi {
  preview: (idleSeconds: number) => Promise<BoardArchivePreview>
  /** Resolves with the per-owner tallies of one confirmed batch. */
  apply: (
    targets: readonly ArchiveTargetVM[],
    token: string,
    options: { dispose: boolean },
  ) => Promise<ArchiveApplyOutcome>
  /** Resolves with the number of sessions returned to the board. */
  unarchive: (targets: readonly ArchiveTargetVM[] | 'all') => Promise<number>
}

const THRESHOLD_ORDER: readonly ArchiveThresholdToken[] = ['30m', '2h', '24h', 'custom']

function thresholdLabel(token: ArchiveThresholdToken): string {
  if (token === '30m') return BOARD_STRINGS.archive.threshold30m
  if (token === '2h') return BOARD_STRINGS.archive.threshold2h
  if (token === '24h') return BOARD_STRINGS.archive.threshold24h
  return BOARD_STRINGS.archive.thresholdCustom
}

/** Message text for a rejected round-trip; never surfaces a raw stack. */
function failureText(template: string, err: unknown): string {
  const reason = err instanceof Error && err.message !== '' ? err.message : String(err)
  return formatTemplate(template, { reason })
}

type DialogPhase = 'idle' | 'previewing' | 'review' | 'applying' | 'done'

export interface ArchiveDialogProps {
  open: boolean
  onClose: () => void
  api: BoardArchiveApi
  /** True when at least one candidate could accept a real dsh dispose. */
  disposeSupported: boolean
  /** Called after a successful apply so the owner can pull a fresh snapshot. */
  onArchived: () => void
  nowMs: number
}

/**
 * Threshold → preview → confirm, with the candidate list checked by default
 * (the operator excludes rather than includes: the preview already applied
 * the idle+dead filter, so the common case is "yes, all of these").
 */
export function ArchiveDialog(props: ArchiveDialogProps): ReactElement {
  const [threshold, setThreshold] = useState<ArchiveThresholdToken>(DEFAULT_ARCHIVE_THRESHOLD)
  const [customMinutes, setCustomMinutes] = useState('')
  const [phase, setPhase] = useState<DialogPhase>('idle')
  const [preview, setPreview] = useState<BoardArchivePreview | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [dispose, setDispose] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<ArchiveApplyOutcome | null>(null)

  const idleSeconds = resolveArchiveSeconds(threshold, customMinutes)
  const candidates = preview?.candidates ?? []
  const selectedTargets = candidates.filter((card) => selected.has(cardKey(card)))
  const disposableSelected = countDisposable(selectedTargets)
  const offerDispose = shouldOfferDispose(props.disposeSupported, selectedTargets)

  const reset = (): void => {
    setPhase('idle')
    setPreview(null)
    setSelected(new Set())
    setDispose(false)
    setError(null)
    setOutcome(null)
  }

  const close = (): void => {
    reset()
    props.onClose()
  }

  const runPreview = (): void => {
    if (idleSeconds === null || phase === 'previewing' || phase === 'applying') return
    setPhase('previewing')
    setError(null)
    props.api.preview(idleSeconds).then(
      (result) => {
        setPreview(result)
        setSelected(new Set(result.candidates.map(cardKey)))
        setPhase('review')
      },
      (err: unknown) => {
        setError(failureText(BOARD_STRINGS.archive.previewFailed, err))
        setPhase('idle')
      },
    )
  }

  const runApply = (): void => {
    if (preview === null || selectedTargets.length === 0 || phase === 'applying') return
    setPhase('applying')
    setError(null)
    props.api
      .apply(
        selectedTargets.map((card) => ({ agent: card.agent, sessionId: card.sessionId })),
        preview.token,
        // A dispose checkbox left ticked from an earlier selection must not
        // survive a selection that has nothing to dispose.
        { dispose: dispose && offerDispose },
      )
      .then(
        (result) => {
          setOutcome(result)
          setPhase('done')
          props.onArchived()
        },
        (err: unknown) => {
          // The token is single-use: a failed apply cannot be retried with
          // the same preview, so fall back to the threshold step.
          setError(failureText(BOARD_STRINGS.archive.failed, err))
          setPreview(null)
          setSelected(new Set())
          setPhase('idle')
        },
      )
  }

  const toggle = (card: SessionCardVM): void => {
    setSelected((current) => {
      const next = new Set(current)
      const key = cardKey(card)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <Modal
      open={props.open}
      onClose={close}
      title={BOARD_STRINGS.archive.title}
      closeLabel={BOARD_STRINGS.archive.cancel}
      description={BOARD_STRINGS.archive.explain}
    >
      <div className={styles['archiveBody']} data-testid="agent-sidecar-archive-dialog">
        <div className={styles['archiveControls']}>
          <label className={styles['control']}>
            {BOARD_STRINGS.archive.threshold}
            <select
              className={styles['select']}
              value={threshold}
              disabled={phase === 'previewing' || phase === 'applying'}
              onChange={(ev) => {
                setThreshold(ev.target.value as ArchiveThresholdToken)
                setPreview(null)
                setPhase('idle')
              }}
              data-testid="agent-sidecar-archive-threshold"
            >
              {THRESHOLD_ORDER.map((token) => (
                <option key={token} value={token}>
                  {thresholdLabel(token)}
                </option>
              ))}
            </select>
          </label>
          {threshold === 'custom' && (
            <label className={styles['control']}>
              <input
                type="number"
                min={1}
                className={styles['archiveNumber']}
                value={customMinutes}
                onChange={(ev) => {
                  setCustomMinutes(ev.target.value)
                  setPreview(null)
                  setPhase('idle')
                }}
                data-testid="agent-sidecar-archive-custom"
              />
              {BOARD_STRINGS.archive.customMinutes}
            </label>
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={idleSeconds === null || phase === 'previewing' || phase === 'applying'}
            onClick={runPreview}
            data-testid="agent-sidecar-archive-preview"
          >
            {phase === 'previewing'
              ? BOARD_STRINGS.archive.previewing
              : BOARD_STRINGS.archive.preview}
          </Button>
        </div>

        {error !== null && (
          <div className={styles['banner']} data-tone="danger" role="alert">
            {error}
          </div>
        )}

        {phase === 'done' && outcome !== null && (
          <div className={styles['banner']} data-tone="warn" role="status">
            {formatTemplate(BOARD_STRINGS.archive.done, { n: outcome.archived })}
            {outcome.disposed > 0 && (
              <span>
                {formatTemplate(BOARD_STRINGS.archive.doneDisposed, { n: outcome.disposed })}
              </span>
            )}
            {outcome.disposeFailed > 0 && (
              <span data-testid="agent-sidecar-archive-dispose-failed">
                {formatTemplate(BOARD_STRINGS.archive.doneDisposeFailed, {
                  n: outcome.disposeFailed,
                })}
              </span>
            )}
          </div>
        )}

        {preview !== null && candidates.length === 0 && (
          <div className={styles['archiveEmpty']} role="status">
            {BOARD_STRINGS.archive.previewEmpty}
          </div>
        )}

        {candidates.length > 0 && (
          <>
            <div className={styles['archiveSummary']}>
              <span>
                {formatTemplate(BOARD_STRINGS.archive.previewCount, {
                  n: candidates.length,
                  selected: selectedTargets.length,
                })}
              </span>
              <span className={styles['spacer']} />
              <Button
                size="sm"
                variant="ghost"
                onClick={() => { setSelected(new Set(candidates.map(cardKey))) }}
              >
                {BOARD_STRINGS.archive.selectAll}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => { setSelected(new Set()) }}
              >
                {BOARD_STRINGS.archive.selectNone}
              </Button>
            </div>
            <ul className={styles['archiveList']}>
              {candidates.map((card) => (
                <li className={styles['archiveRow']} key={cardKey(card)}>
                  <label className={styles['archiveRowLabel']}>
                    <input
                      type="checkbox"
                      className={styles['checkbox']}
                      checked={selected.has(cardKey(card))}
                      onChange={() => { toggle(card) }}
                    />
                    <span className={styles['glyph']} aria-hidden>
                      {agentGlyph(card.agent)}
                    </span>
                    <span className={styles['archiveRowTitle']} title={card.title}>
                      {card.title.trim() === '' ? BOARD_STRINGS.card.untitled : card.title}
                    </span>
                    <span className={styles['archiveRowMeta']}>
                      {abbreviateSessionId(card.sessionId)}
                    </span>
                    <span className={styles['archiveRowMeta']}>
                      {formatRelativeTime(card.updatedAtMs, props.nowMs)}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            {offerDispose && (
              <label className={styles['control']} title={BOARD_STRINGS.archive.disposeHint}>
                <input
                  type="checkbox"
                  className={styles['checkbox']}
                  checked={dispose}
                  onChange={(ev) => { setDispose(ev.target.checked) }}
                  data-testid="agent-sidecar-archive-dispose"
                />
                {formatTemplate(BOARD_STRINGS.archive.dispose, { n: disposableSelected })}
              </label>
            )}
            <div className={styles['archiveActions']}>
              <Button size="sm" variant="ghost" onClick={close}>
                {BOARD_STRINGS.archive.cancel}
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={selectedTargets.length === 0 || phase === 'applying'}
                onClick={runApply}
                data-testid="agent-sidecar-archive-confirm"
              >
                {phase === 'applying'
                  ? BOARD_STRINGS.archive.confirming
                  : formatTemplate(BOARD_STRINGS.archive.confirm, { n: selectedTargets.length })}
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}

export interface ArchivedSectionProps {
  rows: readonly ArchivedCardVM[]
  onUnarchive: (targets: readonly ArchiveTargetVM[] | 'all') => Promise<number>
  /** Called after a successful restore so the owner can refresh the board. */
  onRestored: () => void
  nowMs: number
}

/** Collapsed-by-default drawer listing what the board is currently hiding. */
export function ArchivedSection(props: ArchivedSectionProps): ReactElement | null {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  if (props.rows.length === 0) return null
  const rows = sortArchived(props.rows)

  const restore = (targets: readonly ArchiveTargetVM[] | 'all', busyKey: string): void => {
    if (busy !== null) return
    setBusy(busyKey)
    setError(null)
    props.onUnarchive(targets).then(
      () => {
        setBusy(null)
        props.onRestored()
      },
      (err: unknown) => {
        setBusy(null)
        setError(failureText(BOARD_STRINGS.archived.restoreFailed, err))
      },
    )
  }

  return (
    <section
      {...surfaceProps('board-card', styles['archivedPanel'])}
      data-testid="agent-sidecar-archived"
    >
      <div className={styles['archivedHead']}>
        <button
          type="button"
          className={styles['groupHead']}
          aria-expanded={open}
          title={open ? BOARD_STRINGS.archived.collapse : BOARD_STRINGS.archived.expand}
          onClick={() => { setOpen((visible) => !visible) }}
          data-testid="agent-sidecar-archived-toggle"
        >
          <span className={styles['chevron']} aria-hidden>
            {open ? '▾' : '▸'}
          </span>
          <span className={styles['groupName']}>
            {formatTemplate(BOARD_STRINGS.archived.summary, { n: rows.length })}
          </span>
        </button>
        {open && (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy !== null}
            onClick={() => { restore('all', 'all') }}
            data-testid="agent-sidecar-archived-restore-all"
          >
            {busy === 'all'
              ? BOARD_STRINGS.archived.restoring
              : BOARD_STRINGS.archived.restoreAll}
          </Button>
        )}
      </div>
      {error !== null && (
        <div className={styles['banner']} data-tone="danger" role="alert">
          {error}
        </div>
      )}
      {open && (
        <ul className={styles['archiveList']}>
          {rows.map((row) => {
            const key = cardKey(row)
            return (
              <li className={styles['archiveRow']} key={key}>
                <span className={styles['glyph']} aria-hidden>
                  {agentGlyph(row.agent)}
                </span>
                <span className={styles['archiveRowTitle']} title={row.title}>
                  {row.title.trim() === '' ? BOARD_STRINGS.card.untitled : row.title}
                </span>
                <Pill>{archiveReasonLabel(row.archiveReason)}</Pill>
                <span className={styles['archiveRowMeta']}>
                  {formatTemplate(BOARD_STRINGS.archived.archivedAt, {
                    time: formatRelativeTime(row.archivedAtMs, props.nowMs),
                  })}
                </span>
                <span className={styles['spacer']} />
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy !== null}
                  onClick={() => {
                    restore([{ agent: row.agent, sessionId: row.sessionId }], key)
                  }}
                >
                  {busy === key
                    ? BOARD_STRINGS.archived.restoring
                    : BOARD_STRINGS.archived.restore}
                </Button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export { ARCHIVE_THRESHOLD_SECONDS }
