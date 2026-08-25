/**
 * `/sidecar` slash command (design §4.c `ctx.commands` row / M2 交付,
 * T4.6): a client-owned quick status overview of the sidecar — daemon
 * state, connection health, working/waiting counts, and the top-N active
 * sessions grouped by project — plus a pointer to the full board tab.
 *
 * MECHANISM (source audit, code-first): this dsh version has a CLIENT-side
 * slash-command extension point. The web GUI's slash menu is served by the
 * harness `ui-commands` client package, whose `CommandUiRuntime` service
 * (registered as `commandUi`) accepts client-owned contributions:
 *
 * - `commandUi.register({ name, description, available, ui })` adds one
 *   slash-menu entry whose behavior lives entirely on the client (no host
 *   descriptor) — merged with the host catalog by name, collisions fail
 *   loud, duplicate contributions throw at registration
 *   (harness `packages/client/ui-commands/src/client/contract.ts` +
 *   `service.ts`; ecosystem precedent for the host-side flavor:
 *   `dsh-agent-teams/src/command.ts`).
 * - The only `ui.kind` this dsh version supports is `popupSelect`:
 *   an async `options(session, signal)` provider plus `onSelect`. The
 *   `/sidecar` overview therefore presents as a popup card of rows; its
 *   board row opens Agent Center while informational rows remain inert.
 *
 * The `commandUi` service type is NOT part of the published plugin SDK
 * (same situation as `settingsScope` in ./index.ts), so this module keeps
 * STRUCTURAL mirrors of the harness contract, verified against the source
 * above. Registration goes through `ctx.inject(['commandUi'], …)` — lazy,
 * like the design's `ctx.commands` row: a composition without the service
 * simply never gains the command.
 *
 * DATA: the overview reuses the existing client data layer (`fetchState`,
 * ./api.ts) and the board's pure derivations (./board/logic.ts). No new
 * backend endpoint. The snapshot→overview derivation is the pure, fully
 * unit-testable {@link buildOverview}.
 *
 * Wiring note (S5 integration wave): call {@link registerSidecarCommand}
 * from the client `apply`; this module performs no self-registration.
 *
 * @module
 */

import { fetchState, isApiError } from './api.ts'
import type { RequestOptions, SessionView, StateSnapshot } from './api.ts'
import {
  abbreviateSessionId,
  agentGlyph,
  countWorking,
  deriveWidgetConnection,
  groupSessions,
  normalizeStatus,
} from './board/logic.ts'
import type {
  DaemonStateToken,
  SessionCardVM,
  SessionStatusToken,
  WidgetConnection,
} from './board/logic.ts'
import {
  acquireWithHandoff,
  isRegistrationCollision,
} from './lifecycle/handoff.ts'
import { tCommand } from './locales/command.ts'
import type { CenterNavigation } from './navigation/center.ts'

/** The slash command name (without the leading slash). */
export const SIDECAR_COMMAND_NAME = 'sidecar'

/** Default cap on session rows listed in the overview (top-N truncation). */
export const DEFAULT_OVERVIEW_TOP_N = 5

// ---------------------------------------------------------------------------
// Overview model (the pure snapshot→structure derivation contract).
// ---------------------------------------------------------------------------

/** One session line of the overview. */
export interface OverviewSessionRow {
  agent: string
  /** Single-character agent marker (board vocabulary). */
  glyph: string
  sessionId: string
  /** Head…tail abbreviation for display. */
  shortId: string
  status: SessionStatusToken
  statusLabel: string
  /** Session title with the untitled fallback applied. */
  title: string
  relativeTime: string
}

/** One project section of the overview (board grouping order). */
export interface OverviewProjectGroup {
  /** Raw project path; '' for the unknown-project bucket. */
  key: string
  /** Display label (path basename, or the localized unknown-project name). */
  label: string
  /** Full path; '' when unknown. */
  fullPath: string
  rows: OverviewSessionRow[]
}

/** Guidance block shown when the sidecar is offline or unreachable. */
export interface OverviewGuidance {
  kind: 'unreachable' | 'daemon-failed' | 'daemon-defer'
  title: string
  hint: string
}

/** The full overview structure `/sidecar` renders from. */
export interface OverviewModel {
  /** False only when no snapshot could be fetched at all. */
  reachable: boolean
  /** Daemon supervisor state; null when unreachable. */
  daemonState: DaemonStateToken | null
  daemonLabel: string
  connection: WidgetConnection
  connectionLabel: string
  workingCount: number
  waitingCount: number
  /** Every session in the snapshot (dead included). */
  totalCount: number
  /** Active (non-dead) sessions, grouped by project, capped at top N. */
  groups: OverviewProjectGroup[]
  /** Active sessions dropped by the top-N cap. */
  truncatedCount: number
  /** Non-null when offline/unreachable (design: 「sidecar 未连接」引导). */
  guidance: OverviewGuidance | null
  /** Pointer to the full board tab. */
  boardHint: string
}

/** Tunables of {@link buildOverview} (all injectable for tests). */
export interface OverviewComputeOptions {
  /** Clock (epoch ms); defaults to Date.now() at call time. */
  nowMs?: number
  /** Session-row cap; defaults to {@link DEFAULT_OVERVIEW_TOP_N}. */
  topN?: number
}

// ---------------------------------------------------------------------------
// Pure derivation helpers.
// ---------------------------------------------------------------------------

const MINUTE_MS = 60_000
const HOUR_MS = 3_600_000
const DAY_MS = 86_400_000

/**
 * Coarse relative time over the command locale segment (same thresholds as
 * the board's formatRelativeTime, which is bound to the zh-only board
 * table and therefore not reused here).
 */
function relativeTime(thenMs: number, nowMs: number): string {
  if (!Number.isFinite(thenMs)) return ''
  const delta = nowMs - thenMs
  if (delta < MINUTE_MS) return tCommand('command.time.justNow')
  if (delta < HOUR_MS) {
    return tCommand('command.time.minutesAgo', { n: Math.floor(delta / MINUTE_MS) })
  }
  if (delta < DAY_MS) {
    return tCommand('command.time.hoursAgo', { n: Math.floor(delta / HOUR_MS) })
  }
  return tCommand('command.time.daysAgo', { n: Math.floor(delta / DAY_MS) })
}

/** Wire SessionView → board card VM (epoch seconds → epoch ms boundary). */
function toCardVM(view: SessionView): SessionCardVM {
  return {
    agent: view.agent,
    sessionId: view.session_id,
    status: view.status,
    title: view.title,
    project: view.project,
    updatedAtMs: view.updated_at * 1000,
    lastEvent:
      view.last_event === null
        ? null
        : { kind: view.last_event.kind, text: view.last_event.text },
    gap: view.gap,
  }
}

/** Card VM → overview row (labels resolved in the active locale). */
function toRow(card: SessionCardVM, nowMs: number): OverviewSessionRow {
  const status = normalizeStatus(card.status)
  const title = card.title.trim()
  return {
    agent: card.agent,
    glyph: agentGlyph(card.agent),
    sessionId: card.sessionId,
    shortId: abbreviateSessionId(card.sessionId),
    status,
    statusLabel: tCommand(`command.status.${status}`),
    title: title === '' ? tCommand('command.untitled') : title,
    relativeTime: relativeTime(card.updatedAtMs, nowMs),
  }
}

/** Offline/unreachable guidance for the given daemon situation. */
function deriveGuidance(daemonState: DaemonStateToken | null): OverviewGuidance | null {
  if (daemonState === null) {
    return {
      kind: 'unreachable',
      title: tCommand('command.unreachable'),
      hint: tCommand('command.unreachableHint'),
    }
  }
  if (daemonState === 'failed') {
    return {
      kind: 'daemon-failed',
      title: tCommand('command.offlineFailed'),
      hint: tCommand('command.offlineFailedHint'),
    }
  }
  if (daemonState === 'defer') {
    return {
      kind: 'daemon-defer',
      title: tCommand('command.offlineDefer'),
      hint: tCommand('command.offlineDeferHint'),
    }
  }
  return null
}

/**
 * Pure snapshot→overview derivation. `null` means the state endpoint was
 * unreachable (daemon offline / plugin host down): the model degrades to
 * the 「sidecar 未连接」 guidance. A snapshot with daemon failed/defer keeps
 * its (last-snapshot) sessions and counts — same honesty posture as the
 * board's degraded banner — with the matching guidance attached.
 *
 * Grouping and ordering reuse the board's pure logic: groups by project
 * (most recent first, unknown-project bucket last), cards by status rank
 * then recency. The top-N cap walks groups in that order; groups beyond
 * the cap are dropped and their sessions counted in `truncatedCount`.
 * Dead sessions are never listed (the overview is an "active sessions"
 * glance) but still count into `totalCount`.
 */
export function buildOverview(
  snapshot: StateSnapshot | null,
  opts: OverviewComputeOptions = {},
): OverviewModel {
  const boardHint = tCommand('command.boardHint')
  if (snapshot === null) {
    return {
      reachable: false,
      daemonState: null,
      daemonLabel: tCommand('command.daemon.unknown'),
      connection: 'off',
      connectionLabel: tCommand('command.connection.off'),
      workingCount: 0,
      waitingCount: 0,
      totalCount: 0,
      groups: [],
      truncatedCount: 0,
      guidance: deriveGuidance(null),
      boardHint,
    }
  }

  const nowMs = opts.nowMs ?? Date.now()
  const topN = opts.topN ?? DEFAULT_OVERVIEW_TOP_N
  const daemonState = snapshot.daemon.state
  const sessions = snapshot.board.sessions
  const connection = deriveWidgetConnection(daemonState, snapshot.board.streamHealth)

  const active = sessions
    .filter((view) => normalizeStatus(view.status) !== 'dead')
    .map(toCardVM)

  const groups: OverviewProjectGroup[] = []
  let remaining = Math.max(0, topN)
  let truncatedCount = 0
  for (const group of groupSessions(active)) {
    if (remaining <= 0) {
      truncatedCount += group.cards.length
      continue
    }
    const taken = group.cards.slice(0, remaining)
    truncatedCount += group.cards.length - taken.length
    remaining -= taken.length
    groups.push({
      key: group.key,
      label: group.key === '' ? tCommand('command.unknownProject') : group.label,
      fullPath: group.fullPath,
      rows: taken.map((card) => toRow(card, nowMs)),
    })
  }

  return {
    reachable: true,
    daemonState,
    daemonLabel: tCommand(`command.daemon.${daemonState}`),
    connection,
    connectionLabel: tCommand(`command.connection.${connection}`),
    workingCount: countWorking(sessions),
    waitingCount: sessions.filter((view) => normalizeStatus(view.status) === 'waiting').length,
    totalCount: sessions.length,
    groups,
    truncatedCount,
    guidance: deriveGuidance(daemonState),
    boardHint,
  }
}

// ---------------------------------------------------------------------------
// popupSelect rendering (overview model → option rows).
// ---------------------------------------------------------------------------

/** Structural mirror of ui-commands `SelectOption` (contract.ts). */
export interface SidecarSelectOption {
  readonly id: string
  readonly label: string
  readonly detail?: string
  readonly active?: boolean
}

/**
 * Render the overview as popupSelect rows (stable unique ids). Row order:
 * daemon/connection status, offline guidance (when any), counts or the
 * empty notice, session rows in group order (project carried in the
 * detail line), the truncation marker, and the board-tab pointer.
 */
export function overviewToOptions(model: OverviewModel): SidecarSelectOption[] {
  const options: SidecarSelectOption[] = [
    {
      id: 'daemon',
      label: tCommand('command.daemonRow', { state: model.daemonLabel }),
      detail: model.connectionLabel,
    },
  ]
  if (model.guidance !== null) {
    options.push({
      id: `guidance:${model.guidance.kind}`,
      label: model.guidance.title,
      detail: model.guidance.hint,
    })
  }
  if (model.reachable) {
    if (model.totalCount === 0) {
      options.push({ id: 'empty', label: tCommand('command.noSessions') })
    } else {
      options.push({
        id: 'counts',
        label: tCommand('command.countsRow', {
          working: model.workingCount,
          waiting: model.waitingCount,
        }),
        detail: tCommand('command.countsDetail', { total: model.totalCount }),
      })
    }
    for (const group of model.groups) {
      for (const row of group.rows) {
        options.push({
          id: `session:${row.sessionId}`,
          label: `${row.glyph} ${row.title}`,
          detail: tCommand('command.sessionDetail', {
            project: group.label,
            status: row.statusLabel,
            time: row.relativeTime,
          }),
        })
      }
    }
    if (model.truncatedCount > 0) {
      options.push({
        id: 'truncated',
        label: tCommand('command.truncated', { n: model.truncatedCount }),
      })
    }
  }
  options.push({ id: 'board', label: model.boardHint })
  return options
}

// ---------------------------------------------------------------------------
// Command contribution (structural mirrors of the ui-commands contract).
// ---------------------------------------------------------------------------

/** Structural mirror of ui-commands `CommandUiSpec` (popupSelect kind). */
export interface SidecarCommandUiSpec {
  readonly kind: 'popupSelect'
  options(session: unknown, signal: AbortSignal): Promise<readonly SidecarSelectOption[]>
  onSelect(option: SidecarSelectOption, session: unknown): void | Promise<void>
}

/** Structural mirror of ui-commands `CommandContribution`. */
export interface SidecarCommandContribution {
  readonly name: string
  readonly description: string
  available(session: unknown): boolean
  readonly ui: SidecarCommandUiSpec
}

/** The `commandUi.register` face this module consumes (contract.ts). */
export interface CommandRegistryFace {
  register(contribution: SidecarCommandContribution): () => void
}

/** Injectable seams (tests fake the data layer and the clock). */
export interface SidecarCommandDeps {
  /** State fetcher; defaults to the shared data layer's fetchState. */
  fetchState?: (opts?: RequestOptions) => Promise<StateSnapshot>
  /** Clock (epoch ms); defaults to Date.now. */
  now?: () => number
  /** Session-row cap; defaults to {@link DEFAULT_OVERVIEW_TOP_N}. */
  topN?: number
  /** Opens Agent Center when the board option is selected. */
  openCenter?: CenterNavigation
}

/**
 * Build the `/sidecar` client command contribution. `options` fetches a
 * fresh snapshot per popup open; an abort (popup closed) propagates so the
 * shell can drop the stale request, while every other failure degrades to
 * the unreachable-guidance overview instead of throwing into the shell.
 * `description` is a live getter so a locale switch after registration
 * still reaches the slash menu's next candidate pass.
 */
export function createSidecarCommandContribution(
  deps: SidecarCommandDeps = {},
): SidecarCommandContribution {
  const doFetch = deps.fetchState ?? fetchState
  const now = deps.now ?? Date.now
  return {
    name: SIDECAR_COMMAND_NAME,
    get description(): string {
      return tCommand('command.description')
    },
    // The glance works in every session state; nothing to gate on.
    available: () => true,
    ui: {
      kind: 'popupSelect',
      options: async (_session, signal) => {
        let snapshot: StateSnapshot | null = null
        try {
          snapshot = await doFetch({ signal })
        } catch (err) {
          if (isApiError(err) && err.kind === 'aborted') throw err
          console.error('agent-sidecar: /sidecar state fetch failed', err)
        }
        const overviewOpts: OverviewComputeOptions = { nowMs: now() }
        if (deps.topN !== undefined) overviewOpts.topN = deps.topN
        return overviewToOptions(buildOverview(snapshot, overviewOpts))
      },
      onSelect: (option) => {
        if (option.id === 'board') deps.openCenter?.()
      },
    },
  }
}

// ---------------------------------------------------------------------------
// Registration.
// ---------------------------------------------------------------------------

/**
 * Minimal mount-context face ({@link registerSidecarCommand}); the real
 * `ClientContext` satisfies it structurally (cordis `ctx.inject`).
 */
export interface CommandMountContext {
  inject(
    deps: readonly string[],
    callback: (ctx: unknown) => (() => void) | void,
  ): unknown
}

/**
 * Register `/sidecar` once the `commandUi` service is available (lazy, per
 * the design's `ctx.commands` consumption row — a composition without the
 * slash-menu runtime simply never gains the command).
 *
 * HMR handoff: a duplicate contribution means the old fiber still owns the
 * name. The new injected fiber retries briefly, then registers its own fresh
 * contribution after the old disposer runs. Foreign squatters time out.
 */
export function registerSidecarCommand(
  ctx: CommandMountContext,
  deps: SidecarCommandDeps = {},
): void {
  try {
    ctx.inject(['commandUi'], (injected) => {
      const { commandUi } = injected as { commandUi: CommandRegistryFace }
      return acquireWithHandoff(
        () => commandUi.register(createSidecarCommandContribution(deps)),
        {
          isCollision: isRegistrationCollision,
          onError: (error) => {
            console.error('agent-sidecar: /sidecar command registration failed', error)
          },
          onTimeout: () => {
            console.error('agent-sidecar: /sidecar command handoff timed out')
          },
        },
      )
    })
  } catch (err) {
    console.error('agent-sidecar: commandUi injection failed', err)
  }
}
