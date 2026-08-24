/**
 * Unit tests for the board view-model logic (src/client/board/logic.ts).
 * Pure functions only — no React, no DOM, node environment. Rendering
 * correctness is validated by the integration task against a real dsh web.
 */

import { describe, expect, it } from 'vitest'
import {
  abbreviateSessionId,
  agentGlyph,
  badgeHoverTitle,
  buildBoardViewModel,
  compareCards,
  countByStatus,
  countWorking,
  deriveBadge,
  deriveBanner,
  deriveDaemonBadge,
  deriveEmptyState,
  deriveWidgetConnection,
  filterSessions,
  formatAbsoluteShort,
  formatRelativeTime,
  formatTemplate,
  groupSessions,
  isSessionVisible,
  normalizeStatus,
  projectDisplayName,
  sliceCardsForDisplay,
  statusRank,
  streamHealthTone,
  timeWindowLabel,
  widgetTitle,
  GROUP_CARD_LIMIT,
  type BoardFilterState,
  type SessionCardVM,
} from '../src/client/board/logic.ts'
import { BOARD_STRINGS } from '../src/client/board/strings.ts'

const NOW = 1_700_000_000_000
const HOUR = 3_600_000
const MINUTE = 60_000

function card(overrides: Partial<SessionCardVM> = {}): SessionCardVM {
  return {
    agent: 'claude',
    sessionId: 'sess-1',
    status: 'waiting',
    title: '标题',
    project: '/home/u/proj-a',
    updatedAtMs: NOW - MINUTE,
    lastEvent: null,
    gap: false,
    ...overrides,
  }
}

const defaultFilters: BoardFilterState = { timeWindowHours: 48, showDead: false }

// ---------------------------------------------------------------------------

describe('normalizeStatus / statusRank', () => {
  it('accepts the four known statuses, tolerating case and whitespace', () => {
    expect(normalizeStatus('working')).toBe('working')
    expect(normalizeStatus(' Waiting ')).toBe('waiting')
    expect(normalizeStatus('IDLE')).toBe('idle')
    expect(normalizeStatus('dead')).toBe('dead')
  })

  it('maps anything else to unknown', () => {
    expect(normalizeStatus('running')).toBe('unknown')
    expect(normalizeStatus('')).toBe('unknown')
  })

  it('ranks working > waiting > idle > unknown > dead', () => {
    const ranks = ['working', 'waiting', 'idle', 'unknown', 'dead'] as const
    for (let i = 1; i < ranks.length; i += 1) {
      expect(statusRank(ranks[i - 1]!)).toBeLessThan(statusRank(ranks[i]!))
    }
  })
})

describe('time-window filtering', () => {
  it('hides non-working sessions strictly beyond the window, keeps the boundary', () => {
    const atEdge = card({ updatedAtMs: NOW - 48 * HOUR })
    const beyond = card({ updatedAtMs: NOW - 48 * HOUR - 1 })
    expect(isSessionVisible(atEdge, defaultFilters, NOW)).toBe(true)
    expect(isSessionVisible(beyond, defaultFilters, NOW)).toBe(false)
  })

  it('always keeps working sessions, even outside the window', () => {
    const old = card({ status: 'working', updatedAtMs: NOW - 500 * HOUR })
    expect(isSessionVisible(old, defaultFilters, NOW)).toBe(true)
  })

  it('keeps sessions with a future updatedAt (clock skew)', () => {
    expect(isSessionVisible(card({ updatedAtMs: NOW + HOUR }), defaultFilters, NOW)).toBe(true)
  })

  it('hides dead sessions unless showDead', () => {
    const dead = card({ status: 'dead', updatedAtMs: NOW - MINUTE })
    expect(isSessionVisible(dead, defaultFilters, NOW)).toBe(false)
    expect(isSessionVisible(dead, { ...defaultFilters, showDead: true }, NOW)).toBe(true)
  })

  it('still windows out old dead sessions when showDead is on', () => {
    const oldDead = card({ status: 'dead', updatedAtMs: NOW - 100 * HOUR })
    expect(isSessionVisible(oldDead, { ...defaultFilters, showDead: true }, NOW)).toBe(false)
  })

  it('disables the age filter for non-positive or non-finite windows', () => {
    const ancient = card({ updatedAtMs: NOW - 10_000 * HOUR })
    expect(isSessionVisible(ancient, { timeWindowHours: 0, showDead: false }, NOW)).toBe(true)
    expect(isSessionVisible(ancient, { timeWindowHours: -5, showDead: false }, NOW)).toBe(true)
    expect(isSessionVisible(ancient, { timeWindowHours: Number.NaN, showDead: false }, NOW)).toBe(
      true,
    )
    // showDead still applies even without an age window.
    const dead = card({ status: 'dead' })
    expect(isSessionVisible(dead, { timeWindowHours: 0, showDead: false }, NOW)).toBe(false)
  })

  it('filterSessions preserves input order among survivors', () => {
    const a = card({ sessionId: 'a' })
    const b = card({ sessionId: 'b', updatedAtMs: NOW - 100 * HOUR })
    const c = card({ sessionId: 'c' })
    expect(filterSessions([a, b, c], defaultFilters, NOW).map((s) => s.sessionId)).toEqual([
      'a',
      'c',
    ])
  })
})

describe('status-only filter (UX-01)', () => {
  const board = [
    card({ sessionId: 'w', status: 'working', updatedAtMs: NOW - 500 * HOUR }),
    card({ sessionId: 'a', status: 'waiting' }),
    card({ sessionId: 'b', status: 'Waiting ', updatedAtMs: NOW - 100 * HOUR }),
    card({ sessionId: 'i', status: 'idle' }),
    card({ sessionId: 'd', status: 'dead' }),
  ]

  it('shows exactly the matching status, ignoring window and showDead', () => {
    const waitingOnly = filterSessions(
      board,
      { ...defaultFilters, statusFilter: 'waiting' },
      NOW,
    )
    // 'b' is outside the 48h window yet visible: the badge count and the
    // filtered board must agree on the whole-board answer.
    expect(waitingOnly.map((s) => s.sessionId)).toEqual(['a', 'b'])
    const workingOnly = filterSessions(
      board,
      { ...defaultFilters, statusFilter: 'working' },
      NOW,
    )
    expect(workingOnly.map((s) => s.sessionId)).toEqual(['w'])
  })

  it('absent statusFilter keeps the normal window rules', () => {
    expect(
      isSessionVisible(card({ status: 'idle' }), { ...defaultFilters }, NOW),
    ).toBe(true)
  })

  it('countByStatus normalizes raw statuses; the VM carries both counts', () => {
    expect(countByStatus(board, 'waiting')).toBe(2)
    expect(countByStatus(board, 'working')).toBe(1)
    const vm = buildBoardViewModel({
      sessions: board,
      filters: defaultFilters,
      daemonState: 'hosted',
      streamHealth: 'ok',
      lastReconcileAtMs: NOW,
      nowMs: NOW,
    })
    // Counts answer for the WHOLE board, not just the visible window.
    expect(vm.workingCount).toBe(1)
    expect(vm.waitingCount).toBe(2)
  })
})

describe('sliceCardsForDisplay (UX-02 / UX-20 truncation)', () => {
  const cards = (statuses: string[]): Array<{ status: string; id: number }> =>
    statuses.map((status, id) => ({ status, id }))

  it('returns everything at or under the limit', () => {
    const input = cards(['working', 'idle', 'idle'])
    expect(sliceCardsForDisplay(input, 3, false)).toEqual({
      shown: input,
      hiddenCount: 0,
    })
  })

  it('cuts to the limit and reports the hidden tail', () => {
    const input = cards(['waiting', 'idle', 'idle', 'idle', 'idle'])
    const { shown, hiddenCount } = sliceCardsForDisplay(input, 2, false)
    expect(shown.map((c) => c.id)).toEqual([0, 1])
    expect(hiddenCount).toBe(3)
  })

  it('never cuts inside the leading working/waiting run', () => {
    const input = cards(['working', 'Waiting', 'waiting', 'idle', 'idle'])
    const { shown, hiddenCount } = sliceCardsForDisplay(input, 2, false)
    expect(shown.map((c) => c.id)).toEqual([0, 1, 2])
    expect(hiddenCount).toBe(2)
  })

  it('expanded or non-positive limits disable truncation', () => {
    const input = cards(['idle', 'idle', 'idle'])
    expect(sliceCardsForDisplay(input, 1, true).hiddenCount).toBe(0)
    expect(sliceCardsForDisplay(input, 0, false).hiddenCount).toBe(0)
  })

  it('the board group limit is a sane default', () => {
    expect(GROUP_CARD_LIMIT).toBeGreaterThan(0)
  })
})

describe('grouping and ordering', () => {
  it('buckets empty/whitespace projects under 未知项目 with key ""', () => {
    const groups = groupSessions([card({ project: '' }), card({ project: '  ', sessionId: 's2' })])
    expect(groups).toHaveLength(1)
    expect(groups[0]!.key).toBe('')
    expect(groups[0]!.label).toBe(BOARD_STRINGS.unknownProject)
    expect(groups[0]!.cards).toHaveLength(2)
  })

  it('orders groups by most recent card desc, unknown project always last', () => {
    const groups = groupSessions([
      card({ project: '/p/old', updatedAtMs: NOW - 10 * HOUR }),
      card({ project: '', updatedAtMs: NOW, sessionId: 'u' }),
      card({ project: '/p/new', updatedAtMs: NOW - HOUR, sessionId: 'n' }),
    ])
    expect(groups.map((g) => g.key)).toEqual(['/p/new', '/p/old', ''])
  })

  it('sorts cards inside a group by status rank, then recency, then id', () => {
    const groups = groupSessions([
      card({ sessionId: 'idle-new', status: 'idle', updatedAtMs: NOW }),
      card({ sessionId: 'work-old', status: 'working', updatedAtMs: NOW - 5 * HOUR }),
      card({ sessionId: 'wait-b', status: 'waiting', updatedAtMs: NOW - HOUR }),
      card({ sessionId: 'wait-a', status: 'waiting', updatedAtMs: NOW - HOUR }),
    ])
    expect(groups[0]!.cards.map((c) => c.sessionId)).toEqual([
      'work-old',
      'wait-a',
      'wait-b',
      'idle-new',
    ])
  })

  it('compareCards places dead after unknown statuses', () => {
    const dead = card({ status: 'dead' })
    const weird = card({ status: 'mystery' })
    expect(compareCards(weird, dead)).toBeLessThan(0)
  })
})

describe('badge derivation', () => {
  it('maps known statuses to tone + Chinese label', () => {
    expect(deriveBadge('working', false)).toMatchObject({
      status: 'working',
      tone: 'success',
      label: BOARD_STRINGS.status.working,
      attention: null,
      attentionLabel: null,
    })
    expect(deriveBadge('waiting', false).tone).toBe('warn')
    expect(deriveBadge('idle', false).tone).toBe('neutral')
    expect(deriveBadge('dead', false).tone).toBe('muted')
  })

  it('keeps the raw text as label for unknown statuses (never invents a state)', () => {
    const badge = deriveBadge('resuming', false)
    expect(badge.status).toBe('unknown')
    expect(badge.label).toBe('resuming')
    expect(deriveBadge('  ', false).label).toBe(BOARD_STRINGS.status.unknown)
  })

  it('flags only the per-session gap; no global stale marker at card level (UX-18)', () => {
    const flagged = deriveBadge('working', true)
    expect(flagged.attention).toBe('gap')
    expect(flagged.attentionLabel).toBe(BOARD_STRINGS.attention.gap)
    expect(deriveBadge('working', false).attention).toBeNull()
    expect(deriveBadge('working', false).attentionLabel).toBeNull()
  })
})

describe('badgeHoverTitle', () => {
  it('contains the disclaimer, the raw observed value, and the reconcile time', () => {
    const title = badgeHoverTitle('Working', NOW - 5 * MINUTE, NOW)
    expect(title).toContain(BOARD_STRINGS.card.observedDisclaimer)
    expect(title).toContain('Working')
    expect(title).toContain('5 分钟前')
  })

  it('reports 尚未对账 when there has been no reconcile', () => {
    expect(badgeHoverTitle('idle', null, NOW)).toContain(BOARD_STRINGS.card.neverReconciled)
  })
})

describe('formatRelativeTime boundaries', () => {
  it('is 刚刚 below one minute, including negative deltas', () => {
    expect(formatRelativeTime(NOW, NOW)).toBe(BOARD_STRINGS.time.justNow)
    expect(formatRelativeTime(NOW - MINUTE + 1, NOW)).toBe(BOARD_STRINGS.time.justNow)
    expect(formatRelativeTime(NOW + 10 * MINUTE, NOW)).toBe(BOARD_STRINGS.time.justNow)
  })

  it('stays relative below 24h, exact at the minute/hour boundaries', () => {
    expect(formatRelativeTime(NOW - MINUTE, NOW)).toBe('1 分钟前')
    expect(formatRelativeTime(NOW - HOUR + 1, NOW)).toBe('59 分钟前')
    expect(formatRelativeTime(NOW - HOUR, NOW)).toBe('1 小时前')
    expect(formatRelativeTime(NOW - 24 * HOUR + 1, NOW)).toBe('23 小时前')
  })

  it('switches to the absolute short date from 24h on (UX-13)', () => {
    for (const age of [24 * HOUR, 40 * 24 * HOUR]) {
      const thenMs = NOW - age
      expect(formatRelativeTime(thenMs, NOW)).toBe(formatAbsoluteShort(thenMs))
      expect(formatRelativeTime(thenMs, NOW)).not.toContain('天前')
    }
  })

  it('renders empty for non-finite input', () => {
    expect(formatRelativeTime(Number.NaN, NOW)).toBe('')
  })
})

describe('formatAbsoluteShort', () => {
  it('renders zero-padded local MM-DD HH:mm', () => {
    // 2024-02-03 04:05 local time, built from local components so the
    // expectation holds in every timezone the suite runs in.
    const thenMs = new Date(2024, 1, 3, 4, 5).getTime()
    expect(formatAbsoluteShort(thenMs)).toBe('02-03 04:05')
  })
})

describe('timeWindowLabel', () => {
  it('renders whole days as 天 and everything else as 小时', () => {
    expect(timeWindowLabel(6)).toBe('6 小时')
    expect(timeWindowLabel(36)).toBe('36 小时')
    expect(timeWindowLabel(48)).toBe('2 天')
    expect(timeWindowLabel(168)).toBe('7 天')
  })
})

describe('top bar / banner / empty state', () => {
  it('daemon badge: connected states are green, failed is red', () => {
    expect(deriveDaemonBadge('adopted')).toEqual({
      tone: 'success',
      label: BOARD_STRINGS.daemon.adopted,
    })
    expect(deriveDaemonBadge('hosted').tone).toBe('success')
    expect(deriveDaemonBadge('failed').tone).toBe('danger')
    expect(deriveDaemonBadge('backoff').tone).toBe('warn')
    expect(deriveDaemonBadge('defer').tone).toBe('warn')
  })

  it('stream tone maps ok/degraded/unknown', () => {
    expect(streamHealthTone('ok')).toBe('success')
    expect(streamHealthTone('degraded')).toBe('warn')
    expect(streamHealthTone('unknown')).toBe('neutral')
  })

  it('banner: failed outranks degraded; unknown alone raises none', () => {
    expect(deriveBanner('failed', 'degraded')).toEqual({
      tone: 'danger',
      text: BOARD_STRINGS.banner.daemonFailed,
    })
    expect(deriveBanner('hosted', 'degraded')).toEqual({
      tone: 'warn',
      text: BOARD_STRINGS.banner.streamDegraded,
    })
    expect(deriveBanner('hosted', 'ok')).toBeNull()
    expect(deriveBanner('probe', 'unknown')).toBeNull()
  })

  it('empty state: null when anything is visible', () => {
    expect(deriveEmptyState('failed', 3, 5)).toBeNull()
  })

  it('empty state priority: failed > defer > filtered > no-sessions', () => {
    expect(deriveEmptyState('failed', 0, 5)?.kind).toBe('daemon-failed')
    expect(deriveEmptyState('defer', 0, 0)?.kind).toBe('daemon-defer')
    expect(deriveEmptyState('hosted', 0, 5)?.kind).toBe('filtered')
    expect(deriveEmptyState('hosted', 0, 0)?.kind).toBe('no-sessions')
  })
})

describe('presentation helpers', () => {
  it('agentGlyph knows the six agent families and falls back to a dot', () => {
    expect(agentGlyph('dsh')).toBe('◆')
    expect(agentGlyph('Claude')).toBe('✳')
    expect(agentGlyph('cursor-cli')).toBe(agentGlyph('cursor'))
    expect(agentGlyph('somebody-new')).toBe('●')
  })

  it('abbreviateSessionId keeps short ids and middle-truncates long ones', () => {
    expect(abbreviateSessionId('short-id')).toBe('short-id')
    const long = 'aaaaaaaaaaaabbbbbbbbbbbbcccccc'
    const short = abbreviateSessionId(long)
    expect(short).toBe('aaaaaaaaaaaa…cccccc')
    expect(short.length).toBeLessThan(long.length)
  })

  it('projectDisplayName takes the basename and handles edge shapes', () => {
    expect(projectDisplayName('/home/u/proj-a')).toBe('proj-a')
    expect(projectDisplayName('/home/u/proj-a/')).toBe('proj-a')
    expect(projectDisplayName('C:\\work\\repo')).toBe('repo')
    expect(projectDisplayName('plain-name')).toBe('plain-name')
    expect(projectDisplayName('')).toBe(BOARD_STRINGS.unknownProject)
  })

  it('formatTemplate resolves placeholders and leaves unknown ones intact', () => {
    expect(formatTemplate('{n} 个会话', { n: 3 })).toBe('3 个会话')
    expect(formatTemplate('{missing}!', {})).toBe('{missing}!')
  })
})

describe('widget derivation', () => {
  it('connection: green only for adopted/hosted + ok stream', () => {
    expect(deriveWidgetConnection('adopted', 'ok')).toBe('ok')
    expect(deriveWidgetConnection('hosted', 'ok')).toBe('ok')
    expect(deriveWidgetConnection('hosted', 'degraded')).toBe('degraded')
    expect(deriveWidgetConnection('probe', 'unknown')).toBe('degraded')
    expect(deriveWidgetConnection('backoff', 'ok')).toBe('degraded')
    expect(deriveWidgetConnection('failed', 'ok')).toBe('off')
  })

  it('countWorking counts only normalized working sessions', () => {
    expect(
      countWorking([
        { status: 'working' },
        { status: 'Working ' },
        { status: 'waiting' },
        { status: 'dead' },
      ]),
    ).toBe(2)
  })

  it('widgetTitle stays quiet at zero and adds the count when nonzero', () => {
    const quiet = widgetTitle('ok', 0)
    expect(quiet).toContain(BOARD_STRINGS.widget.connection.ok)
    expect(quiet).not.toContain('工作中')
    expect(widgetTitle('ok', 2)).toContain('2 个会话工作中')
  })
})

describe('buildBoardViewModel (pipeline)', () => {
  it('filters, groups, derives, and counts in one pass', () => {
    const vm = buildBoardViewModel({
      sessions: [
        card({ sessionId: 'w1', status: 'working', project: '/p/a', gap: true }),
        card({ sessionId: 'i1', status: 'idle', project: '' }),
        card({ sessionId: 'old', status: 'waiting', updatedAtMs: NOW - 100 * HOUR }),
        card({ sessionId: 'd1', status: 'dead' }),
      ],
      filters: defaultFilters,
      daemonState: 'hosted',
      streamHealth: 'ok',
      lastReconcileAtMs: NOW - 10_000,
      nowMs: NOW,
    })
    expect(vm.totalCount).toBe(4)
    expect(vm.visibleCount).toBe(2) // 'old' windowed out, 'd1' dead-hidden
    expect(vm.workingCount).toBe(1)
    expect(vm.emptyState).toBeNull()
    expect(vm.banner).toBeNull()
    expect(vm.groups.map((g) => g.key)).toEqual(['/p/a', ''])
    const working = vm.groups[0]!.cards[0]!
    expect(working.badge.attention).toBe('gap')
    expect(working.glyph).toBe('✳')
    expect(working.relativeTime).toBe('1 分钟前')
    expect(working.hoverTitle).toContain(BOARD_STRINGS.card.observedDisclaimer)
  })

  it('produces the filtered empty state when everything is hidden', () => {
    const vm = buildBoardViewModel({
      sessions: [card({ status: 'dead' })],
      filters: defaultFilters,
      daemonState: 'hosted',
      streamHealth: 'ok',
      lastReconcileAtMs: null,
      nowMs: NOW,
    })
    expect(vm.visibleCount).toBe(0)
    expect(vm.groups).toEqual([])
    expect(vm.emptyState?.kind).toBe('filtered')
  })

  it('keeps the degraded notice in the banner only — cards stay clean (UX-18)', () => {
    const vm = buildBoardViewModel({
      sessions: [card()],
      filters: defaultFilters,
      daemonState: 'adopted',
      streamHealth: 'degraded',
      lastReconcileAtMs: NOW,
      nowMs: NOW,
    })
    expect(vm.banner?.tone).toBe('warn')
    expect(vm.groups[0]!.cards[0]!.badge.attention).toBeNull()
    expect(vm.streamTone).toBe('warn')
  })
})
