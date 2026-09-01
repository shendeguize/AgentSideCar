/**
 * Detail-page survivability and header facts (workstream C).
 *
 * Two claims are pinned here. First, timeline degradation is scoped to the
 * timeline body: a session whose sources all failed still renders its header,
 * its back button, and its ids — losing the events must not also strand the
 * operator on a content-free page. Second, every header fact is rendered only
 * when its input exists, so an adapter that cannot report a birth time shows
 * no span row instead of a fabricated one.
 *
 * Node environment throughout — components go through
 * `renderToStaticMarkup`.
 */

import { describe, expect, it, vi } from 'vitest'
import { createElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { SessionDetail, type SessionDetailHeaderVM } from '../src/client/detail/SessionDetail.tsx'
import {
  applyTimelinePage,
  createTimelineVM,
  type TimelinePageWire,
  type TimelineVM,
} from '../src/client/detail/logic.ts'
import { SessionStore } from '../src/session-store.ts'

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
const NOW_S = NOW_MS / 1000

const HEADER: SessionDetailHeaderVM = {
  agent: 'claude',
  title: 'refactor the scanner',
  project: '/tmp/project',
  status: 'idle',
}

const SOURCES = {
  dshLive: false,
  dshCold: false,
  sidecarReplay: true,
  sidecarBuffer: false,
} as const

function page(kinds: readonly string[], nextCursor: string | null): TimelinePageWire {
  return {
    sessionId: 's1',
    entries: kinds.map((kind, index) => ({
      origin: 'sidecar' as const,
      seq: index + 1,
      ts: NOW_MS - (kinds.length - index) * 60_000,
      kind,
      text: `${kind} body`,
      extra: null,
    })),
    cursor: null,
    nextCursor,
    sources: { ...SOURCES },
  }
}

function timeline(kinds: readonly string[]): TimelineVM {
  return applyTimelinePage(createTimelineVM('s1'), page(kinds, null))
}

function renderDetail(
  header: SessionDetailHeaderVM,
  vm: TimelineVM,
  extra: Record<string, unknown> = {},
): string {
  return renderToStaticMarkup(createElement(SessionDetail, {
    sessionId: 's1',
    header,
    timeline: vm,
    loading: false,
    error: null,
    hasMore: false,
    listening: false,
    onLoadMore: () => {},
    onToggleListen: () => {},
    onClose: () => {},
    nowMs: NOW_MS,
    ...extra,
  }))
}

describe('detail header survives timeline degradation', () => {
  it('keeps the header and back button when the boundary replaces the body', () => {
    const replaced = (_body: ReactElement): ReactElement =>
      createElement('div', { 'data-testid': 'agent-sidecar-timeline-degraded' }, 'all sources failed')
    const html = renderDetail(HEADER, timeline([]), { timelineBoundary: replaced })

    expect(html).toContain('agent-sidecar-timeline-degraded')
    expect(html).toContain('agent-sidecar-detail-back')
    expect(html).toContain('agent-sidecar-detail-copy-id')
    expect(html).toContain('refactor the scanner')
  })

  it('scopes the boundary to the body: the header is outside what it can hide', () => {
    let received = ''
    renderDetail(HEADER, timeline(['user']), {
      timelineBoundary: (body: ReactElement) => {
        received = renderToStaticMarkup(body)
        return body
      },
    })

    expect(received).not.toContain('agent-sidecar-detail-back')
    expect(received).not.toContain('agent-sidecar-detail-copy-id')
    expect(received).toContain('agent-sidecar-detail-filter')
  })
})

describe('header facts render only what the adapter reported', () => {
  it('shows last activity, span, model and loaded events when all are known', () => {
    const html = renderDetail(
      {
        ...HEADER,
        updatedAtMs: NOW_MS - 5 * 60_000,
        createdAtMs: NOW_MS - 95 * 60_000,
        model: 'claude-opus-4',
        transcript: '/Users/me/.claude/projects/p/s1.jsonl',
      },
      timeline(['user', 'assistant', 'assistant']),
    )

    expect(html).toContain('最近活动 5 分钟前')
    expect(html).toContain('会话时长 1 小时 30 分')
    expect(html).toContain('模型 claude-opus-4')
    expect(html).toContain('已加载 3 条事件')
    // The by-kind breakdown rides the hover title, not the row itself: the
    // header states one number and keeps the detail one gesture away.
    expect(html).toContain('title="用户消息 1 · 助手回复 2"')
    expect(html).toContain('agent-sidecar-detail-copy-transcript')
    expect(html).toContain('/Users/me/.claude/projects/p/s1.jsonl')
  })

  it('omits span, model and transcript rows when the adapter cannot tell', () => {
    const html = renderDetail(
      { ...HEADER, updatedAtMs: NOW_MS - 5 * 60_000 },
      timeline(['user']),
    )

    expect(html).toContain('5 分钟前')
    expect(html).not.toContain('会话时长')
    expect(html).not.toContain('模型')
    expect(html).not.toContain('agent-sidecar-detail-copy-transcript')
  })

  it('marks the event count as partial while older history is unpaged', () => {
    const partial = applyTimelinePage(createTimelineVM('s1'), page(['user'], 'cursor-1'))

    expect(renderDetail(HEADER, partial)).toContain('已加载 1 条事件(还有更早的历史)')
    expect(renderDetail(HEADER, timeline(['user']))).toContain('已加载 1 条事件<')
  })

  it('makes the project path copyable, and leaves the unknown placeholder inert', () => {
    const withProject = renderDetail(HEADER, timeline([]))
    expect(withProject).toContain('agent-sidecar-detail-copy-project')

    const withoutProject = renderDetail({ ...HEADER, project: '' }, timeline([]))
    expect(withoutProject).not.toContain('agent-sidecar-detail-copy-project')
  })
})

describe('session store metadata projection', () => {
  const row = {
    agent: 'claude',
    session_id: 's1',
    status: 'idle',
    title: 't',
    project: '/p',
    updated_at: NOW_S,
    transcript: '/tmp/s1.jsonl',
    extra: { model: 'claude-opus-4', created_at_epoch: NOW_S - 600 },
  }

  it('projects model and created_at onto the board, but never the transcript path', () => {
    const store = new SessionStore()
    store.applySnapshot([row])

    const [view] = store.getBoardState().sessions
    expect(view?.model).toBe('claude-opus-4')
    expect(view?.created_at).toBe(NOW_S - 600)
    expect(view).not.toHaveProperty('transcript')
  })

  it('attaches the transcript path on the single-session detail lookup', () => {
    const store = new SessionStore()
    store.applySnapshot([row])

    expect(store.getSessionDetail('s1')?.transcript).toBe('/tmp/s1.jsonl')
    expect(store.getSessionDetail('s1')?.model).toBe('claude-opus-4')
    expect(store.getSessionDetail('missing')).toBeNull()
  })

  it('drops a birth time that postdates the last activity', () => {
    const store = new SessionStore()
    store.applySnapshot([
      { ...row, extra: { created_at_epoch: NOW_S + 600 } },
    ])

    expect(store.getBoardState().sessions[0]).not.toHaveProperty('created_at')
  })
})
