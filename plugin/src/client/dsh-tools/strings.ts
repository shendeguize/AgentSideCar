import { createLocaleView } from '../locales/view.ts'

/** Dynamic dsh-tools vocabulary; templates are interpolated by `formatTemplate`. */
export const DSH_TOOLS_STRINGS = createLocaleView({
  lineage: {
    title: 'dshtools.lineage.title',
    loading: 'dshtools.lineage.loading',
    error: 'dshtools.lineage.error',
    empty: 'dshtools.lineage.empty',
    currentBadge: 'dshtools.lineage.currentBadge',
    liveBadge: 'dshtools.lineage.liveBadge',
    notPersistedBadge: 'dshtools.lineage.notPersistedBadge',
    role: {
      ancestor: 'dshtools.lineage.role.ancestor',
      target: 'dshtools.lineage.role.target',
      descendant: 'dshtools.lineage.role.descendant',
    },
    jumpTitle: 'dshtools.lineage.jumpTitle',
    currentTitle: 'dshtools.lineage.currentTitle',
    expand: 'dshtools.lineage.expand',
    collapse: 'dshtools.lineage.collapse',
    nodeCount: 'dshtools.lineage.nodeCount',
    incompleteWithId: 'dshtools.lineage.incompleteWithId',
    incomplete: 'dshtools.lineage.incomplete',
    degrade: {
      notDshTitle: 'dshtools.lineage.degrade.notDshTitle',
      notDshBody: 'dshtools.lineage.degrade.notDshBody',
      queryUnavailableTitle: 'dshtools.lineage.degrade.queryUnavailableTitle',
      queryUnavailableBody: 'dshtools.lineage.degrade.queryUnavailableBody',
      traceFailedTitle: 'dshtools.lineage.degrade.traceFailedTitle',
      traceFailedBody: 'dshtools.lineage.degrade.traceFailedBody',
      unknownTitle: 'dshtools.lineage.degrade.unknownTitle',
      unknownBody: 'dshtools.lineage.degrade.unknownBody',
    },
  },
  search: {
    title: 'dshtools.search.title',
    placeholder: 'dshtools.search.placeholder',
    submit: 'dshtools.search.submit',
    loading: 'dshtools.search.loading',
    error: 'dshtools.search.error',
    empty: 'dshtools.search.empty',
    filterOnlyNotice: 'dshtools.search.filterOnlyNotice',
    projectFilter: 'dshtools.search.projectFilter',
    matchedBy: {
      'full-text': 'dshtools.search.matchedBy.full-text',
      title: 'dshtools.search.matchedBy.title',
      project: 'dshtools.search.matchedBy.project',
      other: 'dshtools.search.matchedBy.other',
    },
    untitled: 'dshtools.search.untitled',
  },
} as const)

export type DshToolsStrings = typeof DSH_TOOLS_STRINGS
