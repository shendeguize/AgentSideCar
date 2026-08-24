/**
 * Unit tests for the project correlation view logic
 * (src/client/board/project-view-logic.ts). Pure functions only — no
 * React, no DOM, node environment (same discipline as board-logic.test.ts).
 *
 * Fixtures mirror the `GET projects` wire contract: fusion.ts
 * `getProjectGroups()` groups with camelCase `UnifiedSession` rows and
 * epoch-MILLISECOND `lastActivityAt` values.
 */

import { describe, expect, it } from 'vitest'
import {
  buildAgentLanes,
  buildProjectViewModel,
  compareProjectSessions,
  deriveAgentBadges,
  normalizeProjectKey,
  PROJECT_VIEW_STRINGS,
  type ProjectGroupVM,
  type ProjectSessionVM,
} from '../src/client/board/project-view-logic.ts'
import { agentGlyph, formatAbsoluteShort, formatTemplate } from '../src/client/board/logic.ts'
import { BOARD_STRINGS } from '../src/client/board/strings.ts'

const NOW = 1_700_000_000_000
const MINUTE = 60_000
const HOUR = 3_600_000

function session(overrides: Partial<ProjectSessionVM> = {}): ProjectSessionVM {
  return {
    agent: 'claude',
    sessionId: 'sess-1',
    status: 'waiting',
    title: '标题',
    lastActivityAt: NOW - MINUTE,
    ...overrides,
  }
}

function group(overrides: Partial<ProjectGroupVM> = {}): ProjectGroupVM {
  const sessions = overrides.sessions ?? [session()]
  return {
    project: '/home/u/proj-a',
    agents: [...new Set(sessions.map((s) => s.agent))].sort(),
    sessions,
    lastActivityAt: sessions.reduce((max, s) => Math.max(max, s.lastActivityAt), 0),
    ...overrides,
  }
}

// ---------------------------------------------------------------------------

describe('normalizeProjectKey', () => {
  it('strips trailing slashes but keeps the root path', () => {
    expect(normalizeProjectKey('/tmp/proj/')).toBe('/tmp/proj')
    expect(normalizeProjectKey('/tmp/proj///')).toBe('/tmp/proj')
    expect(normalizeProjectKey('/tmp/proj')).toBe('/tmp/proj')
    expect(normalizeProjectKey('/')).toBe('/')
    expect(normalizeProjectKey('///')).toBe('/')
  })

  it('maps empty and whitespace-only paths to the unknown bucket', () => {
    expect(normalizeProjectKey('')).toBe('')
    expect(normalizeProjectKey('   ')).toBe('')
  })
})

describe('deriveAgentBadges (cross-agent derivation)', () => {
  it('derives distinct agents from sessions, sorted, with board glyphs', () => {
    const badges = deriveAgentBadges([
      session({ agent: 'dsh' }),
      session({ agent: 'claude', sessionId: 'c1' }),
      session({ agent: 'claude', sessionId: 'c2' }),
      session({ agent: 'cursor', sessionId: 'x1' }),
    ])
    expect(badges.map((b) => b.agent)).toEqual(['claude', 'cursor', 'dsh'])
    expect(badges.map((b) => b.glyph)).toEqual([
      agentGlyph('claude'),
      agentGlyph('cursor'),
      agentGlyph('dsh'),
    ])
  })

  it('yields a single badge for a single-agent project', () => {
    const badges = deriveAgentBadges([session(), session({ sessionId: 's2' })])
    expect(badges).toHaveLength(1)
    expect(badges[0]!.agent).toBe('claude')
  })
})

describe('compareProjectSessions (in-group ordering)', () => {
  it('orders by status rank first: working > waiting > idle > unknown > dead', () => {
    const sorted = [
      session({ sessionId: 'd', status: 'dead' }),
      session({ sessionId: 'u', status: 'mystery' }),
      session({ sessionId: 'w', status: 'working' }),
      session({ sessionId: 'i', status: 'idle' }),
      session({ sessionId: 'a', status: 'waiting' }),
    ].sort(compareProjectSessions)
    expect(sorted.map((s) => s.sessionId)).toEqual(['w', 'a', 'i', 'u', 'd'])
  })

  it('breaks status ties by recency desc, then sessionId asc', () => {
    const sorted = [
      session({ sessionId: 'b-old', lastActivityAt: NOW - HOUR }),
      session({ sessionId: 'new', lastActivityAt: NOW }),
      session({ sessionId: 'a-old', lastActivityAt: NOW - HOUR }),
    ].sort(compareProjectSessions)
    expect(sorted.map((s) => s.sessionId)).toEqual(['new', 'a-old', 'b-old'])
  })
})

describe('buildAgentLanes', () => {
  it('splits sessions into per-agent lanes sorted by agent name', () => {
    const lanes = buildAgentLanes(
      [
        session({ agent: 'dsh', sessionId: 'd1' }),
        session({ agent: 'claude', sessionId: 'c1' }),
        session({ agent: 'dsh', sessionId: 'd2' }),
      ],
      NOW,
    )
    expect(lanes.map((l) => l.agent)).toEqual(['claude', 'dsh'])
    expect(lanes[0]!.glyph).toBe(agentGlyph('claude'))
    expect(lanes[1]!.sessions.map((s) => s.sessionId)).toEqual(['d1', 'd2'])
  })

  it('sorts inside each lane by the board card order (status, recency, id)', () => {
    const lanes = buildAgentLanes(
      [
        session({ sessionId: 'idle-new', status: 'idle', lastActivityAt: NOW }),
        session({ sessionId: 'work-old', status: 'working', lastActivityAt: NOW - 5 * HOUR }),
        session({ sessionId: 'wait-b', status: 'waiting', lastActivityAt: NOW - HOUR }),
        session({ sessionId: 'wait-a', status: 'waiting', lastActivityAt: NOW - HOUR }),
      ],
      NOW,
    )
    expect(lanes).toHaveLength(1)
    expect(lanes[0]!.sessions.map((s) => s.sessionId)).toEqual([
      'work-old',
      'wait-a',
      'wait-b',
      'idle-new',
    ])
  })

  it('derives render-ready fields: badge, glyph, shortId, displayTitle, live/gap', () => {
    const lanes = buildAgentLanes(
      [
        session({
          agent: 'dsh',
          sessionId: 'aaaaaaaaaaaabbbbbbbbbbbbcccccc',
          status: 'working',
          title: '  ',
          live: true,
          gap: true,
        }),
      ],
      NOW,
    )
    const derived = lanes[0]!.sessions[0]!
    expect(derived.badge.status).toBe('working')
    expect(derived.badge.tone).toBe('success')
    expect(derived.badge.label).toBe(BOARD_STRINGS.status.working)
    // The view has no global stream signal: attention reflects gap only.
    expect(derived.badge.attention).toBe('gap')
    expect(derived.glyph).toBe(agentGlyph('dsh'))
    expect(derived.shortId).toBe('aaaaaaaaaaaa…cccccc')
    expect(derived.displayTitle).toBe(PROJECT_VIEW_STRINGS.untitled)
    expect(derived.live).toBe(true)
    expect(derived.gap).toBe(true)
  })

  it('defaults absent live/gap flags to false and keeps unknown raw status text', () => {
    const lanes = buildAgentLanes([session({ status: 'resuming' })], NOW)
    const derived = lanes[0]!.sessions[0]!
    expect(derived.live).toBe(false)
    expect(derived.gap).toBe(false)
    expect(derived.badge.status).toBe('unknown')
    expect(derived.badge.label).toBe('resuming')
    expect(derived.badge.attention).toBeNull()
  })
})

describe('buildProjectViewModel: normalization and merging', () => {
  it('merges wire groups that collapse onto the same normalized key', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({ project: '/tmp/proj', sessions: [session({ agent: 'claude', sessionId: 'c1' })] }),
        group({ project: '/tmp/proj/', sessions: [session({ agent: 'dsh', sessionId: 'd1' })] }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups).toHaveLength(1)
    const merged = vm.groups[0]!
    expect(merged.key).toBe('/tmp/proj')
    expect(merged.sessionCount).toBe(2)
    expect(merged.agentBadges.map((b) => b.agent)).toEqual(['claude', 'dsh'])
  })

  it('deduplicates the same session across merged groups, keeping the freshest copy', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({
          project: '/tmp/proj',
          sessions: [session({ sessionId: 's1', lastActivityAt: NOW - HOUR, title: '旧' })],
        }),
        group({
          project: '/tmp/proj/',
          sessions: [session({ sessionId: 's1', lastActivityAt: NOW, title: '新' })],
        }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups[0]!.sessionCount).toBe(1)
    expect(vm.groups[0]!.lanes[0]!.sessions[0]!.title).toBe('新')
  })

  it('recomputes group lastActivityAt from member sessions (stale wire value ignored)', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({
          project: '/p/a',
          sessions: [session({ lastActivityAt: NOW })],
          lastActivityAt: NOW - 100 * HOUR, // stale wire value
        }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups[0]!.lastActivityAt).toBe(NOW)
  })
})

describe('buildProjectViewModel: group ordering', () => {
  it('orders groups by lastActivityAt desc', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({ project: '/p/old', sessions: [session({ lastActivityAt: NOW - 10 * HOUR })] }),
        group({ project: '/p/new', sessions: [session({ lastActivityAt: NOW - HOUR })] }),
        group({ project: '/p/mid', sessions: [session({ lastActivityAt: NOW - 5 * HOUR })] }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups.map((g) => g.key)).toEqual(['/p/new', '/p/mid', '/p/old'])
  })

  it('breaks activity ties by project key asc', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({ project: '/p/b', sessions: [session({ lastActivityAt: NOW })] }),
        group({ project: '/p/a', sessions: [session({ lastActivityAt: NOW })] }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups.map((g) => g.key)).toEqual(['/p/a', '/p/b'])
  })

  it('keeps the unknown-project group last even when it is the most recent', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({ project: '', sessions: [session({ lastActivityAt: NOW })] }),
        group({ project: '/p/a', sessions: [session({ lastActivityAt: NOW - 10 * HOUR })] }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups.map((g) => g.key)).toEqual(['/p/a', ''])
    const unknown = vm.groups[1]!
    expect(unknown.label).toBe(BOARD_STRINGS.unknownProject)
    expect(unknown.fullPath).toBe('')
  })

  it('labels named groups with the path basename (board rule)', () => {
    const vm = buildProjectViewModel({
      groups: [group({ project: '/home/u/proj-a/' })],
      nowMs: NOW,
    })
    expect(vm.groups[0]!.label).toBe('proj-a')
    expect(vm.groups[0]!.fullPath).toBe('/home/u/proj-a')
  })
})

describe('buildProjectViewModel: cross-agent badge', () => {
  it('exposes the cross-agent label only when 2+ agent kinds participate', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({
          project: '/p/multi',
          sessions: [
            session({ agent: 'dsh', sessionId: 'd1' }),
            session({ agent: 'claude', sessionId: 'c1' }),
            session({ agent: 'cursor', sessionId: 'x1', lastActivityAt: NOW - 10 * HOUR }),
          ],
        }),
        group({ project: '/p/single', sessions: [session({ sessionId: 'only' })] }),
      ],
      nowMs: NOW,
    })
    const multi = vm.groups.find((g) => g.key === '/p/multi')!
    expect(multi.crossAgentLabel).toBe(
      formatTemplate(PROJECT_VIEW_STRINGS.crossAgent, { n: 3 }),
    )
    expect(multi.agentBadges.map((b) => b.agent)).toEqual(['claude', 'cursor', 'dsh'])
    expect(multi.lanes.map((l) => l.agent)).toEqual(['claude', 'cursor', 'dsh'])
    const single = vm.groups.find((g) => g.key === '/p/single')!
    expect(single.crossAgentLabel).toBeNull()
    expect(single.agentBadges).toHaveLength(1)
  })

  it('counts agent kinds, not sessions', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({
          project: '/p/a',
          sessions: [
            session({ agent: 'dsh', sessionId: 'd1' }),
            session({ agent: 'dsh', sessionId: 'd2' }),
            session({ agent: 'claude', sessionId: 'c1' }),
          ],
        }),
      ],
      nowMs: NOW,
    })
    expect(vm.groups[0]!.crossAgentLabel).toBe(
      formatTemplate(PROJECT_VIEW_STRINGS.crossAgent, { n: 2 }),
    )
    expect(vm.groups[0]!.sessionCount).toBe(3)
  })
})

describe('buildProjectViewModel: relative time', () => {
  it('renders card and group relative times from the injected clock', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({
          project: '/p/a',
          sessions: [
            session({ sessionId: 'fresh', status: 'working', lastActivityAt: NOW - 30_000 }),
            session({ sessionId: 'older', lastActivityAt: NOW - 5 * MINUTE }),
          ],
        }),
      ],
      nowMs: NOW,
    })
    const lane = vm.groups[0]!.lanes[0]!
    expect(lane.sessions.map((s) => s.relativeTime)).toEqual([
      BOARD_STRINGS.time.justNow,
      '5 分钟前',
    ])
    // Group recency follows the freshest member.
    expect(vm.groups[0]!.lastActiveLabel).toBe(
      formatTemplate(PROJECT_VIEW_STRINGS.lastActive, { time: BOARD_STRINGS.time.justNow }),
    )
  })

  it('switches units for hour-old groups and absolute dates past 24h (UX-13)', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({ project: '/p/h', sessions: [session({ lastActivityAt: NOW - 3 * HOUR })] }),
        group({ project: '/p/d', sessions: [session({ lastActivityAt: NOW - 49 * HOUR })] }),
      ],
      nowMs: NOW,
    })
    const byKey = new Map(vm.groups.map((g) => [g.key, g]))
    expect(byKey.get('/p/h')!.lastActiveLabel).toContain('3 小时前')
    expect(byKey.get('/p/d')!.lastActiveLabel).toContain(
      formatAbsoluteShort(NOW - 49 * HOUR),
    )
  })
})

describe('buildProjectViewModel: empty state and counts', () => {
  it('returns the empty state exactly when there is no group', () => {
    const vm = buildProjectViewModel({ groups: [], nowMs: NOW })
    expect(vm.groups).toEqual([])
    expect(vm.emptyState).toEqual({ ...PROJECT_VIEW_STRINGS.empty })
    expect(vm.projectCount).toBe(0)
    expect(vm.sessionCount).toBe(0)
  })

  it('keeps emptyState null when any group exists', () => {
    const vm = buildProjectViewModel({ groups: [group()], nowMs: NOW })
    expect(vm.emptyState).toBeNull()
  })

  it('derives counts, per-group session labels, and the summary line', () => {
    const vm = buildProjectViewModel({
      groups: [
        group({
          project: '/p/a',
          sessions: [session({ sessionId: 's1' }), session({ sessionId: 's2' })],
        }),
        group({ project: '/p/b', sessions: [session({ sessionId: 's3' })] }),
      ],
      nowMs: NOW,
    })
    expect(vm.projectCount).toBe(2)
    expect(vm.sessionCount).toBe(3)
    expect(vm.summaryLabel).toBe(
      formatTemplate(PROJECT_VIEW_STRINGS.summary, { projects: 2, sessions: 3 }),
    )
    const byKey = new Map(vm.groups.map((g) => [g.key, g]))
    expect(byKey.get('/p/a')!.sessionCountLabel).toBe(
      formatTemplate(PROJECT_VIEW_STRINGS.sessionCount, { n: 2 }),
    )
  })
})
