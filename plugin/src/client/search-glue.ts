/**
 * Cross-agent search data glue (T5.10b): a framework-free store feeding
 * the controlled SearchPanel. Normalization (matchedBy vocabulary, snippet
 * highlighting) stays in dsh-tools/logic.ts — this store owns the query
 * box state and transport orchestration only.
 *
 * Submit model: explicit user submit (no as-you-type dialing). A blank
 * query with no project filter clears the results locally — the host
 * answers 400 `invalid_request` for it, so the store never dials that.
 * Stale results are replaced per settle; a failed submit keeps the last
 * results visible with an error banner (SearchPanel renders error-first).
 *
 * Same store discipline as controller.ts: subscribe/getState for
 * `useSyncExternalStore`, immutable snapshots, dispose() = late no-ops.
 * Out-of-order settles are ignored via a submit ticket.
 *
 * @module
 */

import { isApiError, type RequestOptions } from './api.ts'
import { fetchSearch } from './m3-transport.ts'
import {
  normalizeSearchItems,
  type SearchItemVM,
  type SearchMode,
  type SearchResponseVM,
} from './dsh-tools/logic.ts'

export interface SearchGlueState {
  /** Controlled query box value. */
  query: string
  /** Query echoed by the last settled response ('' before the first). */
  submittedQuery: string
  /** Mode of the last settled response; 'full-text' until told otherwise. */
  mode: SearchMode
  items: SearchItemVM[]
  loading: boolean
  /** Machine reason code of the last failure, or null. */
  error: string | null
  /** Active project filter sent with submits and echoed back, if any. */
  project: string | null
}

export interface SearchStoreOptions {
  fetchSearchFn?: (
    opts?: RequestOptions & { q?: string; project?: string | null; limit?: number },
  ) => Promise<SearchResponseVM>
  /** Result cap passed to the endpoint (server default if unset). */
  limit?: number
  /** Fixed project filter (e.g. a search scoped from the project view). */
  project?: string | null
}

const INITIAL_STATE: SearchGlueState = {
  query: '',
  submittedQuery: '',
  mode: 'full-text',
  items: [],
  loading: false,
  error: null,
  project: null,
}

export class SearchStore {
  private state: SearchGlueState
  private readonly listeners = new Set<() => void>()
  private readonly fetchSearchFn: NonNullable<SearchStoreOptions['fetchSearchFn']>
  private readonly limit: number | undefined
  private disposed = false
  private ticket = 0

  constructor(options: SearchStoreOptions = {}) {
    this.fetchSearchFn = options.fetchSearchFn ?? fetchSearch
    this.limit = options.limit
    this.state = { ...INITIAL_STATE, project: options.project ?? null }
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => { this.listeners.delete(fn) }
  }

  getState = (): SearchGlueState => this.state

  private setState(patch: Partial<SearchGlueState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...patch }
    for (const fn of [...this.listeners]) fn()
  }

  /** Controlled input change (no dialing). */
  setQuery(query: string): void {
    this.setState({ query })
  }

  /** Submit the current query; blank + no project filter clears locally. */
  async submit(): Promise<void> {
    if (this.disposed) return
    const q = this.state.query.trim()
    const project = this.state.project
    if (q === '' && (project === null || project.trim() === '')) {
      this.ticket += 1 // outrun any in-flight settle
      this.setState({
        items: [], submittedQuery: '', loading: false, error: null,
      })
      return
    }
    const ticket = (this.ticket += 1)
    this.setState({ loading: true, error: null })
    try {
      const response = await this.fetchSearchFn({
        q,
        project,
        ...(this.limit !== undefined ? { limit: this.limit } : {}),
      })
      if (this.disposed || ticket !== this.ticket) return
      this.adoptResponse(response)
    } catch (err) {
      if (this.disposed || ticket !== this.ticket) return
      this.setState({
        loading: false,
        error: isApiError(err) ? err.reason : 'network_error',
      })
    }
  }

  /** Apply one settled wire response (public seam for tests/materialize). */
  adoptResponse(response: SearchResponseVM): void {
    this.setState({
      items: normalizeSearchItems(response),
      mode: response.mode,
      submittedQuery: response.query,
      project: response.project,
      loading: false,
      error: null,
    })
  }

  dispose(): void {
    this.disposed = true
    this.listeners.clear()
  }
}
