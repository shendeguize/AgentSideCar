/**
 * Batch-archive surface: threshold resolution, the archived drawer's
 * rendering, the board's entry-point gate, and the api glue that turns
 * wire rows into board view models.
 *
 * Node environment throughout — components go through
 * `renderToStaticMarkup`, transport goes through an injected fetch fake.
 */

import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Board } from '../src/client/board/Board.tsx'
import { ArchivedSection, type BoardArchiveApi } from '../src/client/board/ArchivePanel.tsx'
import { createArchiveApi, ARCHIVE_STATUSES } from '../src/client/board/archive-glue.ts'
import {
  archiveReasonLabel,
  cardKey,
  countDisposable,
  resolveArchiveSeconds,
  shouldOfferDispose,
  sortArchived,
  ARCHIVE_THRESHOLD_SECONDS,
  DEFAULT_ARCHIVE_THRESHOLD,
  MAX_ARCHIVE_SECONDS,
  type ArchivedCardVM,
} from '../src/client/board/logic.ts'
import { BOARD_STRINGS } from '../src/client/board/strings.ts'
import type { FetchLike, RequestInitLike, ResponseLike } from '../src/client/api.ts'

// The published primitives ship untransformed CSS modules; the same stand-in
// the other component tests use keeps this suite in a bare node environment.
vi.mock('@deepseek-ai/dsh-client-ui-primitives', () => ({
  Button: 'button',
  IconChevronDownOutline14: 'svg',
  Input: 'input',
  Modal: 'div',
  Pill: 'span',
  StateDot: 'span',
  writeClipboard: vi.fn(() => Promise.resolve(true)),
}))

const NOW_MS = 1_700_000_000_000

function archivedCard(overrides: Partial<ArchivedCardVM> = {}): ArchivedCardVM {
  return {
    agent: 'claude',
    sessionId: 'session-alpha',
    status: 'idle',
    title: 'refactor the scanner',
    project: '/tmp/project',
    updatedAtMs: NOW_MS - 3 * 3_600_000,
    lastEvent: null,
    gap: false,
    archivedAtMs: NOW_MS - 3_600_000,
    archiveReason: 'batch',
    ...overrides,
  }
}

const INERT_API: BoardArchiveApi = {
  preview: () => Promise.reject(new Error('unused')),
  apply: () => Promise.reject(new Error('unused')),
  unarchive: () => Promise.reject(new Error('unused')),
}

function renderBoard(overrides: Record<string, unknown> = {}): string {
  return renderToStaticMarkup(createElement(Board, {
    daemonState: 'hosted',
    streamHealth: 'ok',
    lastReconcileAtMs: NOW_MS,
    hasSnapshot: true,
    initialLoadFailed: false,
    sessions: [],
    filters: { timeWindowHours: 24, showDead: false },
    onFiltersChange: () => {},
    onRefresh: () => {},
    onSelectSession: () => {},
    returnFocusTarget: null,
    onReturnFocusConsumed: () => {},
    rootRef: () => {},
    onScrollTopChange: () => {},
    nowMs: NOW_MS,
    ...overrides,
  } as Parameters<typeof Board>[0]))
}

describe('archive threshold resolution', () => {
  it('maps every preset to its second count and defaults to the 2h preset', () => {
    expect(resolveArchiveSeconds('30m', '')).toBe(1800)
    expect(resolveArchiveSeconds('2h', '')).toBe(7200)
    expect(resolveArchiveSeconds('24h', '')).toBe(86400)
    expect(DEFAULT_ARCHIVE_THRESHOLD).toBe('2h')
    expect(ARCHIVE_THRESHOLD_SECONDS['2h']).toBe(7200)
  })

  it('accepts an in-range custom value and rejects empty/short/absurd ones', () => {
    expect(resolveArchiveSeconds('custom', '90')).toBe(5400)
    expect(resolveArchiveSeconds('custom', ' 5 ')).toBe(300)
    expect(resolveArchiveSeconds('custom', '')).toBeNull()
    expect(resolveArchiveSeconds('custom', 'abc')).toBeNull()
    expect(resolveArchiveSeconds('custom', '0')).toBeNull()
    expect(resolveArchiveSeconds('custom', String(MAX_ARCHIVE_SECONDS / 60 + 1))).toBeNull()
  })

  it('offers the dispose opt-in only for a capable host AND a dsh selection', () => {
    const dsh = [{ agent: 'dsh' }, { agent: 'claude' }]
    expect(countDisposable(dsh)).toBe(1)
    expect(shouldOfferDispose(true, dsh)).toBe(true)
    // Nothing in the selection can be disposed: the checkbox would lie.
    expect(shouldOfferDispose(true, [{ agent: 'claude' }, { agent: 'codex' }])).toBe(false)
    // The host has no sessions service, so nothing can be disposed at all.
    expect(shouldOfferDispose(false, dsh)).toBe(false)
    expect(shouldOfferDispose(true, [])).toBe(false)
  })

  it('keys cards by agent AND session id, since ids repeat across agents', () => {
    expect(cardKey({ agent: 'claude', sessionId: 's1' }))
      .not.toBe(cardKey({ agent: 'codex', sessionId: 's1' }))
  })

  it('labels the known provenance tokens and echoes anything else verbatim', () => {
    expect(archiveReasonLabel('auto')).toBe(BOARD_STRINGS.archived.reason.auto)
    expect(archiveReasonLabel('batch')).toBe(BOARD_STRINGS.archived.reason.batch)
    expect(archiveReasonLabel('manual')).toBe(BOARD_STRINGS.archived.reason.manual)
    expect(archiveReasonLabel('imported')).toBe('imported')
  })

  it('sorts archived rows newest decision first without mutating the input', () => {
    const rows = [
      archivedCard({ sessionId: 'old', archivedAtMs: 1 }),
      archivedCard({ sessionId: 'new', archivedAtMs: 9 }),
    ]
    expect(sortArchived(rows).map((row) => row.sessionId)).toEqual(['new', 'old'])
    expect(rows.map((row) => row.sessionId)).toEqual(['old', 'new'])
  })
})

describe('board archive entry point', () => {
  it('stays hidden when the host advertises no archive api', () => {
    expect(renderBoard()).not.toContain('agent-sidecar-archive-open')
  })

  it('appears once an archive api is bound', () => {
    const html = renderBoard({ archive: INERT_API })
    expect(html).toContain('agent-sidecar-archive-open')
    expect(html).toContain(BOARD_STRINGS.archive.open)
  })

  it('renders the archived drawer collapsed with its count', () => {
    const html = renderBoard({ archive: INERT_API, archived: [archivedCard()] })
    expect(html).toContain('agent-sidecar-archived-toggle')
    expect(html).toContain('1')
    // Collapsed: the row itself and the bulk restore stay out of the DOM.
    expect(html).not.toContain('agent-sidecar-archived-restore-all')
    expect(html).not.toContain('refactor the scanner')
  })

  it('omits the drawer entirely when nothing is archived', () => {
    expect(renderBoard({ archive: INERT_API, archived: [] }))
      .not.toContain('agent-sidecar-archived')
  })
})

describe('archived section', () => {
  it('renders nothing for an empty registry', () => {
    const html = renderToStaticMarkup(createElement(ArchivedSection, {
      rows: [],
      onUnarchive: () => Promise.resolve(0),
      onRestored: () => {},
      nowMs: NOW_MS,
    }))
    expect(html).toBe('')
  })

  it('shows one summary row per archive decision', () => {
    const html = renderToStaticMarkup(createElement(ArchivedSection, {
      rows: [archivedCard(), archivedCard({ sessionId: 'session-beta' })],
      onUnarchive: () => Promise.resolve(0),
      onRestored: () => {},
      nowMs: NOW_MS,
    }))
    expect(html).toContain('agent-sidecar-archived-toggle')
    expect(html).toContain('2')
  })
})

// ---------------------------------------------------------------------------
// api glue
// ---------------------------------------------------------------------------

function jsonFetch(body: unknown, status = 200): { fetch: FetchLike; seen: RequestInitLike[] } {
  const seen: RequestInitLike[] = []
  const response: ResponseLike = {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  }
  return {
    seen,
    fetch: (_url, init) => {
      seen.push(init)
      return Promise.resolve(response)
    },
  }
}

describe('archive api glue', () => {
  it('asks only for idle/dead candidates and converts the wire clock to ms', async () => {
    const { fetch, seen } = jsonFetch({
      idle_seconds: 7200,
      statuses: ['idle', 'dead'],
      token: 'tok-1',
      count: 1,
      candidates: [{
        agent: 'codex',
        session_id: 'abc',
        status: 'idle',
        title: 'stale run',
        project: '/tmp/p',
        updated_at: 1_700_000_000,
        last_event: null,
        gap: false,
        inject_eligibility: 'unsupported',
      }],
    })
    const api = createArchiveApi({ fetch })

    const preview = await api.preview(7200)

    expect(ARCHIVE_STATUSES).toEqual(['idle', 'dead'])
    expect(JSON.parse(String(seen[0]?.body))).toEqual({
      type: 'archive.preview',
      idleSeconds: 7200,
      statuses: ['idle', 'dead'],
    })
    expect(preview.token).toBe('tok-1')
    expect(preview.candidates).toHaveLength(1)
    expect(preview.candidates[0]?.updatedAtMs).toBe(1_700_000_000_000)
  })

  it('replays the preview token verbatim and reports the archived count', async () => {
    const { fetch, seen } = jsonFetch({ count: 2, requested: 3 })
    const api = createArchiveApi({ fetch })

    const outcome = await api.apply(
      [{ agent: 'claude', sessionId: 'a' }, { agent: 'codex', sessionId: 'b' }],
      'tok-9',
      { dispose: false },
    )

    expect(outcome).toEqual({ archived: 2, disposed: 0, disposeFailed: 0 })
    // One round-trip only: an un-opted-in batch never touches dispose.
    expect(seen).toHaveLength(1)
    expect(JSON.parse(String(seen[0]?.body))).toEqual({
      type: 'archive.apply',
      token: 'tok-9',
      targets: [{ agent: 'claude', sessionId: 'a' }, { agent: 'codex', sessionId: 'b' }],
    })
  })

  it('disposes only the dsh targets, and only when the batch opted in', async () => {
    const seen: RequestInitLike[] = []
    const fetch: FetchLike = (_url, init) => {
      seen.push(init)
      const body = JSON.parse(String(init?.body)) as { type: string }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(
          body.type === 'archive.apply'
            ? { count: 3, requested: 3 }
            : { outcome: 'disposed' },
        ),
      } satisfies ResponseLike)
    }
    const api = createArchiveApi({ fetch })

    const outcome = await api.apply(
      [
        { agent: 'dsh', sessionId: 'd1' },
        { agent: 'claude', sessionId: 'c1' },
        { agent: 'dsh', sessionId: 'd2' },
      ],
      'tok-1',
      { dispose: true },
    )

    expect(outcome).toEqual({ archived: 3, disposed: 2, disposeFailed: 0 })
    // Archive first (the token is single-use), then one dispose per dsh row.
    expect(seen.map((init) => JSON.parse(String(init?.body)).type)).toEqual([
      'archive.apply',
      'dsh.dispose',
      'dsh.dispose',
    ])
    expect(JSON.parse(String(seen[1]?.body)).sessionId).toBe('d1')
    expect(JSON.parse(String(seen[2]?.body)).sessionId).toBe('d2')
  })

  it('counts a refused dispose as a failure instead of failing the batch', async () => {
    const fetch: FetchLike = (_url, init) => {
      const body = JSON.parse(String(init?.body)) as { type: string }
      if (body.type === 'archive.apply') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ count: 2, requested: 2 }),
        } satisfies ResponseLike)
      }
      return Promise.resolve({
        ok: false,
        status: 501,
        json: () => Promise.resolve({ reason: 'dispose_unavailable' }),
      } satisfies ResponseLike)
    }
    const api = createArchiveApi({ fetch })

    // The sessions are hidden either way; only the dispose half is lost.
    expect(await api.apply(
      [{ agent: 'dsh', sessionId: 'd1' }, { agent: 'dsh', sessionId: 'd2' }],
      'tok-2',
      { dispose: true },
    )).toEqual({ archived: 2, disposed: 0, disposeFailed: 2 })
  })

  it('treats an already-gone session as disposed, not as a failure', async () => {
    const fetch: FetchLike = (_url, init) => {
      const body = JSON.parse(String(init?.body)) as { type: string }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(
          body.type === 'archive.apply'
            ? { count: 1, requested: 1 }
            : { outcome: 'not_found' },
        ),
      } satisfies ResponseLike)
    }
    const api = createArchiveApi({ fetch })

    expect(await api.apply([{ agent: 'dsh', sessionId: 'gone' }], 'tok-3', { dispose: true }))
      .toEqual({ archived: 1, disposed: 1, disposeFailed: 0 })
  })

  it('sends the bulk form when unarchiving everything', async () => {
    const { fetch, seen } = jsonFetch({ count: 4 })
    const api = createArchiveApi({ fetch })

    expect(await api.unarchive('all')).toBe(4)
    expect(JSON.parse(String(seen[0]?.body))).toEqual({ type: 'archive.unarchive', all: true })
  })

  it('surfaces the server reason as the rejection message, not an http dump', async () => {
    const { fetch } = jsonFetch({ reason: 'archive_token_expired' }, 409)
    const api = createArchiveApi({ fetch })

    await expect(api.apply([{ agent: 'claude', sessionId: 'a' }], 'stale', { dispose: false }))
      .rejects.toThrowError('archive_token_expired')
  })
})
