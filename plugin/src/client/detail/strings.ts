import { createLocaleView } from '../locales/view.ts'

/** Dynamic session-detail vocabulary; templates are interpolated by `formatTemplate`. */
export const DETAIL_STRINGS = createLocaleView({
  header: {
    close: 'detail.header.close',
    listenOn: 'detail.header.listenOn',
    listenOff: 'detail.header.listenOff',
    listenHint: 'detail.header.listenHint',
    refresh: 'detail.header.refresh',
    refreshing: 'detail.header.refreshing',
    refreshHint: 'detail.header.refreshHint',
    copyIdTitle: 'detail.header.copyIdTitle',
    copied: 'detail.header.copied',
    untitled: 'detail.header.untitled',
    unknownProject: 'detail.header.unknownProject',
    observedDisclaimer: 'detail.header.observedDisclaimer',
  },
  status: {
    working: 'detail.status.working',
    waiting: 'detail.status.waiting',
    idle: 'detail.status.idle',
    dead: 'detail.status.dead',
    unknown: 'detail.status.unknown',
  },
  sources: {
    title: 'detail.sources.title',
    dshLive: 'detail.sources.dshLive',
    dshCold: 'detail.sources.dshCold',
    sidecarReplay: 'detail.sources.sidecarReplay',
    sidecarBuffer: 'detail.sources.sidecarBuffer',
    none: 'detail.sources.none',
  },
  kind: {
    user: 'detail.kind.user',
    assistant: 'detail.kind.assistant',
    thinking: 'detail.kind.thinking',
    toolCall: 'detail.kind.toolCall',
    toolResult: 'detail.kind.toolResult',
    turn: 'detail.kind.turn',
    step: 'detail.kind.step',
    error: 'detail.kind.error',
    other: 'detail.kind.other',
  },
  gap: {
    label: 'detail.gap.label',
  },
  filter: {
    conversation: 'detail.filter.conversation',
    all: 'detail.filter.all',
    hiddenNotice: 'detail.filter.hiddenNotice',
  },
  timeline: {
    loadMore: 'detail.timeline.loadMore',
    loadingMore: 'detail.timeline.loadingMore',
    noMore: 'detail.timeline.noMore',
    expand: 'detail.timeline.expand',
    collapse: 'detail.timeline.collapse',
    newBadge: 'detail.timeline.newBadge',
    seq: 'detail.timeline.seq',
    hiddenNotice: 'detail.timeline.hiddenNotice',
    showAll: 'detail.timeline.showAll',
    chunkRun: 'detail.timeline.chunkRun',
  },
  states: {
    loadingTitle: 'detail.states.loadingTitle',
    emptyTitle: 'detail.states.emptyTitle',
    emptyHint: 'detail.states.emptyHint',
    errorTitle: 'detail.states.errorTitle',
    errorFallback: 'detail.states.errorFallback',
    errors: {
      session_not_found: 'detail.states.errors.session_not_found',
      invalid_cursor: 'detail.states.errors.invalid_cursor',
      fusion_not_wired: 'detail.states.errors.fusion_not_wired',
      network_error: 'detail.states.errors.network_error',
      request_timeout: 'detail.states.errors.request_timeout',
    },
  },
  time: {
    justNow: 'detail.time.justNow',
    minutesAgo: 'detail.time.minutesAgo',
    hoursAgo: 'detail.time.hoursAgo',
    daysAgo: 'detail.time.daysAgo',
  },
} as const)

export type DetailStrings = typeof DETAIL_STRINGS
