/**
 * UI composition ports for the board and session-detail surfaces.
 *
 * Views depend on these narrow factory seams instead of owning the
 * application assembly contract. The default factory binds the existing
 * stores; callers can provide alternate port implementations in tests or
 * other materializations.
 *
 * @module
 */

import { AnalysisStore } from './analysis-glue.ts'
import { dshDispose, type DisposeOutcome } from './api.ts'
import { createArchiveApi } from './board/archive-glue.ts'
import type { BoardArchiveApi } from './board/ArchivePanel.tsx'
import { DetailStore, type DetailHeaderHint } from './detail-glue.ts'
import type { InjectActions } from './inject-glue.ts'
import type { InjectMode } from './inject/logic.ts'
import type { VerifyProbe } from './inject/verify.ts'
import { ProjectsStore } from './project-glue.ts'
import { SearchStore } from './search-glue.ts'

/** Detail-store capabilities consumed by the session-detail surface. */
export type DetailStorePort = Pick<
  DetailStore,
  | 'subscribe'
  | 'getState'
  | 'open'
  | 'loadMore'
  | 'toggleListen'
  | 'refreshNewest'
  | 'notifySnapshot'
  | 'dispose'
>

/** Search-store capabilities consumed by the detail tools surface. */
export type SearchStorePort = Pick<
  SearchStore,
  'subscribe' | 'getState' | 'setQuery' | 'submit' | 'dispose'
>

/** Analysis-store capabilities consumed by the analysis panel. */
export type AnalysisStorePort = Pick<
  AnalysisStore,
  'subscribe' | 'getState' | 'start' | 'followup' | 'stop' | 'dispose'
>

/** Project-store capabilities consumed by the project view. */
export type ProjectsStorePort = Pick<
  ProjectsStore,
  'subscribe' | 'getState' | 'refresh' | 'notifySnapshot' | 'dispose'
>

/** Optional injection capability consumed by the detail surface. */
export interface InjectUiPort {
  actions: InjectActions
  /** Read when the panel opens so late settings updates are observed. */
  getDefaultMode: () => InjectMode
  /**
   * Read-only probe of the target's newest timeline window, used to check
   * an unknown delivery automatically. Optional: a composition that omits
   * it leaves the unknown result page with its manual hint.
   */
  createVerifyProbe?: (sessionId: string) => VerifyProbe
}

/**
 * Optional dsh session-dispose capability consumed by the detail surface.
 * Absent on compositions without the host sessions service; the detail
 * page then renders no dispose control at all rather than a doomed one.
 */
export interface DisposeUiPort {
  dispose: (sessionId: string) => Promise<DisposeOutcome>
}

/** Dependencies required to assemble one session-detail surface. */
export interface DetailUiPort {
  inject?: InjectUiPort
  dispose?: DisposeUiPort
  /** Live settings-backed analysis gate; defaults remain fail-closed. */
  getAnalysisEnabled: () => boolean
  createDetailStore: (sessionId: string, hint: DetailHeaderHint | null) => DetailStorePort
  createSearchStore: () => SearchStorePort
}

/** Dependencies required by the board and the detail route it opens. */
export interface BoardUiPort {
  detail: DetailUiPort
  createProjectsStore: () => ProjectsStorePort
  /** One tab-scoped analysis conversation, retained across route switches. */
  createAnalysisStore: () => AnalysisStorePort
  /**
   * Batch-archive round-trips. Bound by the default integration; the board
   * still gates the entry point on the daemon advertising an archive policy.
   */
  createArchiveApi?: () => BoardArchiveApi
}

type DetailIntegrationBase = Pick<DetailUiPort, 'inject' | 'getAnalysisEnabled'>

/**
 * The default dispose binding. Always bound here — the host capability
 * flag (`capabilities.dispose`), not the presence of this function, is
 * what decides whether the control is offered.
 */
const defaultDisposePort: DisposeUiPort = {
  dispose: async (sessionId) => {
    try {
      return (await dshDispose(sessionId)).outcome
    } catch {
      // Transport/gate refusals collapse into the same content-free
      // vocabulary the host uses, so the UI has one thing to render.
      return 'failed'
    }
  },
}

/** Bind the production store implementations at the client composition root. */
export function createDefaultIntegration(base: DetailIntegrationBase): BoardUiPort {
  return {
    detail: {
      ...base,
      dispose: defaultDisposePort,
      createDetailStore: (sessionId, hint) => new DetailStore(sessionId, { hint }),
      createSearchStore: () => new SearchStore(),
    },
    createProjectsStore: () => new ProjectsStore(),
    createAnalysisStore: () => new AnalysisStore(),
    createArchiveApi,
  }
}
