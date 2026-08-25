import { createLocaleView } from '../locales/view.ts'

/** Dynamic board vocabulary; template leaves are interpolated by `formatTemplate`. */
export const BOARD_STRINGS = createLocaleView({
  status: {
    working: 'board.status.working',
    waiting: 'board.status.waiting',
    idle: 'board.status.idle',
    dead: 'board.status.dead',
    unknown: 'board.status.unknown',
  },
  attention: {
    gap: 'board.attention.gap',
  },
  daemon: {
    probe: 'board.daemon.probe',
    adopted: 'board.daemon.adopted',
    defer: 'board.daemon.defer',
    reprobe: 'board.daemon.reprobe',
    hosting: 'board.daemon.hosting',
    hosted: 'board.daemon.hosted',
    backoff: 'board.daemon.backoff',
    failed: 'board.daemon.failed',
  },
  stream: {
    ok: 'board.stream.ok',
    degraded: 'board.stream.degraded',
    unknown: 'board.stream.unknown',
  },
  banner: {
    daemonFailed: 'board.banner.daemonFailed',
    streamDegraded: 'board.banner.streamDegraded',
  },
  empty: {
    daemonFailedTitle: 'board.empty.daemonFailedTitle',
    daemonFailedHint: 'board.empty.daemonFailedHint',
    daemonDeferTitle: 'board.empty.daemonDeferTitle',
    daemonDeferHint: 'board.empty.daemonDeferHint',
    filteredTitle: 'board.empty.filteredTitle',
    filteredHint: 'board.empty.filteredHint',
    noSessionsTitle: 'board.empty.noSessionsTitle',
    noSessionsHint: 'board.empty.noSessionsHint',
  },
  topbar: {
    title: 'board.topbar.title',
    refresh: 'board.topbar.refresh',
    refreshing: 'board.topbar.refreshing',
    refreshTitle: 'board.topbar.refreshTitle',
    refreshFailed: 'board.topbar.refreshFailed',
    dismiss: 'board.topbar.dismiss',
    showDead: 'board.topbar.showDead',
    timeWindow: 'board.topbar.timeWindow',
    countWorking: 'board.topbar.countWorking',
    countWaiting: 'board.topbar.countWaiting',
    countTotal: 'board.topbar.countTotal',
    filterByStatusTitle: 'board.topbar.filterByStatusTitle',
    clearStatusFilterTitle: 'board.topbar.clearStatusFilterTitle',
  },
  group: {
    collapseTitle: 'board.group.collapseTitle',
    expandTitle: 'board.group.expandTitle',
    showAll: 'board.group.showAll',
    showLess: 'board.group.showLess',
  },
  card: {
    noEvent: 'board.card.noEvent',
    untitled: 'board.card.untitled',
    observedDisclaimer: 'board.card.observedDisclaimer',
    observedValue: 'board.card.observedValue',
    lastReconcile: 'board.card.lastReconcile',
    neverReconciled: 'board.card.neverReconciled',
    copyId: 'board.card.copyId',
    copied: 'board.card.copied',
  },
  time: {
    justNow: 'board.time.justNow',
    minutesAgo: 'board.time.minutesAgo',
    hoursAgo: 'board.time.hoursAgo',
  },
  timeWindow: {
    hours: 'board.timeWindow.hours',
    days: 'board.timeWindow.days',
  },
  groupCount: 'board.groupCount',
  unknownProject: 'board.unknownProject',
  widget: {
    label: 'board.widget.label',
    connection: {
      ok: 'board.widget.connection.ok',
      degraded: 'board.widget.connection.degraded',
      off: 'board.widget.connection.off',
    },
    working: 'board.widget.working',
  },
} as const)

export type BoardStrings = typeof BOARD_STRINGS
