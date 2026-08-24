/**
 * English dictionary, compile-checked complete against the zh key set (a
 * missing or extra key is a type error; see ./zh.ts for the key-space
 * convention).
 */

import type { SidecarLocaleKey } from './zh.ts'

export const en = {
  // ── card chrome ────────────────────────────────────────────────────────
  'settings.cardTitle': 'Agent Sidecar',
  'settings.cardDescription':
    'Runtime settings for cross-agent session monitoring, injection and bypass analysis.',
  'settings.docsLink': 'Documentation',
  'settings.readOnly': 'The settings document is read-only; edits cannot be saved.',
  'settings.unsaved': 'Unsaved',
  'settings.save': 'Save',
  'settings.saving': 'Saving…',
  'settings.discard': 'Discard',
  'settings.saveFailed': 'Save failed; please retry.',
  'settings.expand': 'Expand',
  'settings.collapse': 'Collapse',
  'settings.invalidNumber': 'Enter an integer no less than {min}',

  // ── daemon lifecycle ───────────────────────────────────────────────────
  'settings.sectionDaemon': 'Daemon lifecycle',
  'settings.daemonPolicyLabel': 'Management policy',
  'settings.daemonPolicyHint':
    'adopt-or-host probes and adopts an existing daemon, else spawns one; adopt-only never spawns; off leaves the lifecycle alone (read-only reconcile still runs).',
  'settings.daemonPolicyAdoptOrHost': 'adopt-or-host (adopt, else spawn)',
  'settings.daemonPolicyAdoptOnly': 'adopt-only (never spawn)',
  'settings.daemonPolicyOff': 'off (unmanaged)',
  'settings.daemonBackoffLimitLabel': 'Backoff limit',
  'settings.daemonBackoffLimitHint':
    'After this many consecutive hosting failures the supervisor stops restarting and trips failed.',
  'settings.daemonStatusLabel': 'Daemon status',
  'settings.daemonStateProbe': 'Probing',
  'settings.daemonStateAdopted': 'Adopted an existing daemon',
  'settings.daemonStateDefer': 'Waiting for the system service',
  'settings.daemonStateReprobe': 'Re-probing',
  'settings.daemonStateHosting': 'Spawning',
  'settings.daemonStateHosted': 'Hosted by the plugin',
  'settings.daemonStateBackoff': 'Backing off before retry',
  'settings.daemonStateFailed': 'Tripped (circuit open)',
  'settings.daemonPidVersion': 'pid {pid} · v{version}',
  'settings.daemonDeferNote':
    'The daemon is managed by a system service (LaunchAgent); the plugin only probes and waits — it never spawns a duplicate or terminates it.',
  'settings.daemonFailedNote':
    'Consecutive hosting failures reached the backoff limit; the board degraded to the last snapshot. Check the sidecar command, then retry.',
  'settings.daemonRetry': 'Retry',

  // ── sidecar invocation ─────────────────────────────────────────────────
  'settings.sectionSidecar': 'Sidecar invocation',
  'settings.sidecarCommandLabel': 'Executable command',
  'settings.sidecarCommandHint':
    'A PATH name, an absolute path, or a space-separated multi-part command (e.g. python3 /path/agent-sidecar.pyz); the plugin never installs the sidecar for you.',
  'settings.sidecarRuntimeDirLabel': 'Runtime directory',
  'settings.sidecarRuntimeDirHint':
    'Empty uses the default ~/.agent_sidecar (honoring AGENT_SIDECAR_RUNTIME_DIR); a non-empty value is passed to spawned daemons via the environment.',

  // ── stream reconciliation ──────────────────────────────────────────────
  'settings.sectionStream': 'Stream reconciliation',
  'settings.streamActiveMsLabel': 'Active cadence (ms)',
  'settings.streamActiveMsHint': 'Status snapshot cadence while any session is working.',
  'settings.streamIdleMsLabel': 'Idle cadence (ms)',
  'settings.streamIdleMsHint': 'Status snapshot cadence while no session is working.',

  // ── injection ──────────────────────────────────────────────────────────
  'settings.sectionInject': 'Message injection',
  'settings.injectEnabledLabel': 'Enable injection',
  'settings.injectEnabledHint':
    'When off, the board hides every inject affordance and the server rejects write actions.',
  'settings.injectDefaultModeLabel': 'Default injection mode',
  'settings.injectDefaultModeHint': 'The mode preselected when the inject panel opens.',
  'settings.injectModeQueue': 'queue (next turn)',
  'settings.injectModeSteer': 'steer (mid-turn)',
  'settings.injectSafetyNote':
    'Safety: injection is off by default; even when enabled, every injection still passes a per-request confirmation dialog — there is no batch or scheduled injection. Not recommended on multi-user hosts.',

  // ── bypass analysis ────────────────────────────────────────────────────
  'settings.sectionAnalysis': 'Bypass analysis',
  'settings.analysisEnabledLabel': 'Enable AI bypass analysis',
  'settings.analysisEnabledHint':
    'Spins up a dsh analysis session over an observed session on demand (consumes model tokens; off by default).',

  // ── board UI ───────────────────────────────────────────────────────────
  'settings.sectionUi': 'Board UI',
  'settings.uiTimeWindowHoursLabel': 'Session time window (hours)',
  'settings.uiTimeWindowHoursHint': 'The board lists only sessions active within this window.',
  'settings.uiShowDeadLabel': 'Show dead sessions',
  'settings.uiShowDeadHint': 'Also list finished (dead) sessions on the board.',

  // ── skill mode ─────────────────────────────────────────────────────────
  'settings.sectionSkill': 'Skill mode',
  'settings.skillProvideLabel': 'Provide the skill in-process',
  'settings.skillProvideHint':
    'Provide the agent-sidecar skill to dsh via registerProvider (enabled in M4; applies after restart).',

  // ── inject panel: chrome & editor (T4.5, design §5.1 view 3) ───────────
  'inject.title': 'Inject message',
  'inject.confirmTitle': 'Confirm injection',
  'inject.close': 'Close',
  'inject.done': 'Done',
  'inject.capabilityOff': 'Injection is not enabled; turn it on in Settings first.',
  'inject.noTarget':
    'No injection target selected; start from a board card or a session detail page.',
  'inject.targetLabel': 'Target',
  'inject.messageLabel': 'Message',
  'inject.messagePlaceholder': 'Message to inject (16 KiB max; never paste secrets)',
  'inject.byteCount': '{bytes} / {limit} bytes',
  'inject.msgEmpty': 'The message must not be empty.',
  'inject.msgNul': 'The message contains an illegal NUL character.',
  'inject.msgTooLarge': 'The message is {bytes} bytes, over the {limit}-byte limit.',
  'inject.modeLabel': 'Injection mode',
  'inject.modeQueue': 'queue (next turn)',
  'inject.modeQueueHint':
    'The message queues and is handled when the target session starts its next turn.',
  'inject.modeSteer': 'steer (mid-turn)',
  'inject.modeSteerHint':
    'The message is injected mid-turn and steers the target session immediately.',
  'inject.argvWarning':
    "cursor-cli target: injection runs through its native subprocess, and the message is visible in this machine's process list while that process lives; never include secrets.",
  'inject.auditNote':
    'This injection is recorded in the sidecar audit log (byte size and content fingerprint; never the message plaintext).',
  'inject.prepare': 'Prepare injection',
  'inject.preparing': 'Validating…',

  // ── inject panel: confirm phase ──────────────────────────────────────
  'inject.planTargetLabel': 'Target snapshot',
  'inject.planStatus': 'Current status: {status}',
  'inject.statusObservedNote':
    'Status is an observed value inferred from persisted data and may lag.',
  'inject.planModeLabel': 'Mode',
  'inject.planPreviewLabel': 'Message digest ({bytes} bytes)',
  'inject.countdown': 'The confirm token expires in {seconds}s',
  'inject.confirmExecute': 'Confirm injection',
  'inject.executing': 'Injecting…',
  'inject.cancel': 'Cancel',
  'inject.tokenExpired':
    'Confirmation timed out and the token is void; prepare the injection again.',

  // ── inject panel: result phase ───────────────────────────────────────
  'inject.resultDelivered': 'Delivered: the message was injected into the target session.',
  'inject.resultFailed': 'Injection failed.',
  'inject.resultUnknown':
    'Outcome unknown: the message may have been delivered. Do NOT retry; check the target session before deciding anything.',
  'inject.resultReplayed':
    'Idempotent replay: this is the earlier result of the same request — no second injection happened.',
  'inject.reprepare': 'Prepare again',

  // ── inject panel: error vocabulary (gateway + transport) ─────────────
  'inject.errInjectDisabled': 'Injection is disabled on the server; enable it in Settings.',
  'inject.errInvalidMessage': 'The message failed server-side validation.',
  'inject.errTargetNotFound':
    'The target session does not exist or left the observation window.',
  'inject.errTargetDead': 'The target session has ended (dead); it cannot be injected.',
  'inject.errTooManyPending':
    'Too many injections are pending confirmation; try again later.',
  'inject.errTokenMissing': 'The confirm token is missing or was never issued; prepare again.',
  'inject.errTokenExpired': 'The confirm token expired; prepare again.',
  'inject.errTokenReused': 'The confirm token was already consumed; prepare again.',
  'inject.errTokenMismatch':
    'The confirmation no longer matches what was prepared; prepare again.',
  'inject.errUnsupportedAgent': 'This agent has no injection path.',
  'inject.errExecutorError': 'The injection path failed while executing.',
  'inject.errTimeout': 'The request timed out without a server receipt.',
  'inject.errAborted': 'The request was cancelled.',
  'inject.errNetwork': 'Network error; the request could not be sent.',
  'inject.errParse': 'The server response could not be parsed.',
  'inject.errGeneric': 'Request failed ({code}).',

  // ── board tab chrome: main-view switcher ───────────────────────────────
  'board.viewBoard': 'Session board',
  'board.viewProjects': 'Projects',

  // ── session detail ───────────────────────────────────────────────────
  'detail.header.close': 'Back to board',
  'detail.header.listenOn': 'Listening',
  'detail.header.listenOff': 'Listen',
  'detail.header.listenHint': 'New events append live and get highlighted while on',
  'detail.header.untitled': '(untitled)',
  'detail.header.unknownProject': 'Unknown project',
  'detail.header.observedDisclaimer':
    'Status is an observed value inferred from persisted data and may lag',
  'detail.status.working': 'Working',
  'detail.status.waiting': 'Waiting',
  'detail.status.idle': 'Idle',
  'detail.status.dead': 'Finished',
  'detail.status.unknown': 'Unknown',
  'detail.sources.title': 'Data sources',
  'detail.sources.dshLive': 'dsh live',
  'detail.sources.dshCold': 'dsh cold read',
  'detail.sources.sidecarReplay': 'sidecar replay',
  'detail.sources.sidecarBuffer': 'sidecar buffer',
  'detail.sources.none': 'Unknown source',
  'detail.kind.user': 'User message',
  'detail.kind.assistant': 'Assistant reply',
  'detail.kind.thinking': 'Thinking',
  'detail.kind.toolCall': 'Tool call',
  'detail.kind.toolResult': 'Tool result',
  'detail.kind.turn': 'Turn',
  'detail.kind.step': 'Step',
  'detail.kind.error': 'Error',
  'detail.kind.other': 'Event',
  'detail.gap.label':
    'Gap: about {n} events may be uncaptured (256-slot queue cap or not persisted)',
  'detail.timeline.loadMore': 'Load older history',
  'detail.timeline.loadingMore': 'Loading…',
  'detail.timeline.noMore': 'Start of the timeline',
  'detail.timeline.expand': 'Expand',
  'detail.timeline.collapse': 'Collapse',
  'detail.timeline.newBadge': 'New',
  'detail.timeline.seq': 'seq {n}',
  'detail.timeline.hiddenNotice': '{n} earlier entries collapsed to stay smooth',
  'detail.timeline.showAll': 'Show all',
  'detail.states.loadingTitle': 'Loading the timeline…',
  'detail.states.emptyTitle': 'No events yet',
  'detail.states.emptyHint': 'This session has no normalized events to show yet.',
  'detail.states.errorTitle': 'Timeline failed to load',
  'detail.states.errorFallback': 'Error code: {reason}',
  'detail.states.errors.session_not_found': 'The session does not exist or is no longer visible',
  'detail.states.errors.invalid_cursor': 'The paging cursor is invalid; reopen the detail view',
  'detail.states.errors.fusion_not_wired': 'This host has no timeline capability enabled',
  'detail.states.errors.network_error': 'Network error; the dsh host is unreachable',
  'detail.states.errors.request_timeout': 'The request timed out',
  'detail.time.justNow': 'just now',
  'detail.time.minutesAgo': '{n} min ago',
  'detail.time.hoursAgo': '{n} h ago',
  'detail.time.daysAgo': '{n} d ago',

  // ── session detail: integration chrome ─────────────────────────────────
  'detail.actions.inject': 'Inject',
  'detail.actions.analyze': 'AI analysis',
  'detail.actions.analyzeDisabledHint':
    'Enable "AI bypass analysis" in Settings to use this',

  // ── dsh deep-query tools ────────────────────────────────────────────────
  'dshtools.lineage.title': 'Session lineage',
  'dshtools.lineage.loading': 'Loading lineage…',
  'dshtools.lineage.error': 'Lineage failed to load',
  'dshtools.lineage.empty': 'No lineage data',
  'dshtools.lineage.currentBadge': 'Current session',
  'dshtools.lineage.liveBadge': 'Live',
  'dshtools.lineage.notPersistedBadge': 'Not persisted',
  'dshtools.lineage.role.ancestor': 'Ancestor',
  'dshtools.lineage.role.target': 'Target',
  'dshtools.lineage.role.descendant': 'Child session',
  'dshtools.lineage.jumpTitle': 'Jump to this session',
  'dshtools.lineage.currentTitle': 'Currently viewing this session',
  'dshtools.lineage.expand': 'Expand',
  'dshtools.lineage.collapse': 'Collapse',
  'dshtools.lineage.nodeCount': '{n} sessions',
  'dshtools.lineage.incompleteWithId':
    'Lineage incomplete: parent session {id} could not be resolved',
  'dshtools.lineage.incomplete': 'Lineage incomplete: part of the parent chain is unresolved',
  'dshtools.lineage.degrade.notDshTitle': 'Lineage/provenance is dsh-session only',
  'dshtools.lineage.degrade.notDshBody':
    "This session comes from an external agent; dsh's lineage and provenance do not apply.",
  'dshtools.lineage.degrade.queryUnavailableTitle': 'dsh lineage service unavailable',
  'dshtools.lineage.degrade.queryUnavailableBody':
    'This dsh composition has no sessionQuery mounted; lineage and provenance are unavailable.',
  'dshtools.lineage.degrade.traceFailedTitle': 'Lineage trace failed',
  'dshtools.lineage.degrade.traceFailedBody': "dsh could not resolve this session's lineage.",
  'dshtools.lineage.degrade.unknownTitle': 'Lineage unavailable',
  'dshtools.lineage.degrade.unknownBody':
    'The backend reports lineage unavailable (reason: {reason}).',
  'dshtools.search.title': 'Session search',
  'dshtools.search.placeholder': 'Search sessions (title / project / full text)',
  'dshtools.search.submit': 'Search',
  'dshtools.search.loading': 'Searching…',
  'dshtools.search.error': 'Search failed',
  'dshtools.search.empty': 'No matching sessions',
  'dshtools.search.filterOnlyNotice':
    'dsh full-text search is unavailable; degraded to title/project filtering',
  'dshtools.search.projectFilter': 'Project filter: {project}',
  'dshtools.search.matchedBy.full-text': 'Full text',
  'dshtools.search.matchedBy.title': 'Title',
  'dshtools.search.matchedBy.project': 'Project',
  'dshtools.search.matchedBy.other': 'Other',
  'dshtools.search.untitled': '(untitled)',

  // ── project correlation view ────────────────────────────────────────────
  'project.title': 'Project correlation',
  'project.summary': '{projects} projects · {sessions} sessions',
  'project.crossAgent': '{n} agent kinds',
  'project.sessionCount': '{n} sessions',
  'project.lastActive': 'Last active {time}',
  'project.liveChip': 'Live',
  'project.untitled': '(untitled)',
  'project.empty.title': 'No project correlation yet',
  'project.empty.hint':
    'No cross-agent project activity within the time window; projects appear here once an agent works inside a project directory.',
  'project.loading': 'Loading project correlation…',
  'project.errorTitle': 'Project correlation failed to load',

  // ── AI bypass analysis panel ────────────────────────────────────────────
  'analysis.title': 'AI analysis',
  'analysis.close': 'Close',
  'analysis.disabledNote':
    'AI bypass analysis is off; enable "AI bypass analysis" in Settings first.',
  'analysis.idleHint':
    'Spin up one dsh bypass-analysis pass over this session on demand (consumes model tokens).',
  'analysis.start': 'Start analysis',
  'analysis.requesting': 'Analyzing… (up to ~60 s)',
  'analysis.exchangeInitial': 'Analysis summary',
  'analysis.followupLabel': 'Follow-up',
  'analysis.truncatedNotice':
    'The input exceeded the budget and was truncated; the analysis covers partial context.',
  'analysis.emptySummary': '(the analysis session returned no summary)',
  'analysis.disclaimerFallback':
    'AI analysis is for reference only; trust the actual session over its conclusions.',
  'analysis.followupPlaceholder': 'Ask a follow-up about this analysis…',
  'analysis.followupSubmit': 'Ask',
  'analysis.answering': 'Answering…',
  'analysis.stop': 'Stop analysis',
  'analysis.stopped': 'Analysis stopped; the analysis session was released.',
  'analysis.restart': 'Analyze again',
  'analysis.noticeTimeout':
    'This follow-up timed out; retry later — the analysis session is kept.',
  'analysis.noticeNetwork':
    'The request could not be sent; retry — the analysis session is kept.',
  'analysis.noticeCancelFailed': 'The stop request did not go through; try again.',
  'analysis.errDisabled': 'AI analysis is disabled on the server; enable it in Settings and retry.',
  'analysis.errUnavailable':
    'This host has no AI analysis capability (agents service unavailable).',
  'analysis.errTargetNotFound': 'The analysis target does not exist or left the observation window.',
  'analysis.errTooManyActive': 'Too many concurrent analysis sessions; try again later.',
  'analysis.errTimeout': 'The analysis timed out and its session was released; start again.',
  'analysis.errCreateFailed': 'Failed to create the analysis session.',
  'analysis.errCancelled': 'The analysis was cancelled.',
  'analysis.errNetwork': 'Network error; the analysis request could not complete.',
  'analysis.errGeneric': 'Analysis failed ({code}).',

  // ── /sidecar slash command (folded from locales/command.ts) ────────────
  'command.description': 'Sidecar status at a glance (daemon, connection, sessions)',
  'command.daemon.probe': 'Probing',
  'command.daemon.adopted': 'Connected · adopted',
  'command.daemon.defer': 'Waiting for the system service',
  'command.daemon.reprobe': 'Re-probing',
  'command.daemon.hosting': 'Starting',
  'command.daemon.hosted': 'Connected · hosted',
  'command.daemon.backoff': 'Restart backoff',
  'command.daemon.failed': 'Offline',
  'command.daemon.unknown': 'State unknown',
  'command.connection.ok': 'Connected',
  'command.connection.degraded': 'Connection unstable',
  'command.connection.off': 'Offline',
  'command.status.working': 'Working',
  'command.status.waiting': 'Waiting',
  'command.status.idle': 'Idle',
  'command.status.dead': 'Finished',
  'command.status.unknown': 'Unknown',
  'command.daemonRow': 'Sidecar · {state}',
  'command.countsRow': '{working} working · {waiting} waiting',
  'command.countsDetail': '{total} sessions total',
  'command.sessionDetail': '{project} · {status} · {time}',
  'command.noSessions': 'No observed sessions yet',
  'command.unknownProject': 'Unknown project',
  'command.untitled': '(untitled)',
  'command.truncated': '{n} more active sessions not listed',
  'command.boardHint': 'Open the "Sidecar" tab in the conversation view for the full board',
  'command.unreachable': 'sidecar is not connected',
  'command.unreachableHint':
    'The state snapshot could not be fetched; check that the agent-sidecar plugin is enabled and the daemon is available, then retry.',
  'command.offlineFailed': 'sidecar is offline (daemon start failures tripped the breaker)',
  'command.offlineFailedHint':
    'Showing the last snapshot. Retry from the settings card, or run agent-sidecar daemon start manually and wait for adoption.',
  'command.offlineDefer': 'Waiting for the system service to start the daemon',
  'command.offlineDeferHint':
    'A LaunchAgent manages the daemon; the plugin only probes and waits. The overview recovers automatically once the service brings it up.',
  'command.time.justNow': 'just now',
  'command.time.minutesAgo': '{n} min ago',
  'command.time.hoursAgo': '{n} h ago',
  'command.time.daysAgo': '{n} d ago',

  // ── better-sidebar mini tab (T6.3) ─────────────────────────────────────
  'sidebar.tabTitle': 'Sidecar',
  'sidebar.countsRow': '{working} working · {waiting} waiting',
  'sidebar.recentTitle': 'Recently active',
  'sidebar.connecting': 'Waiting for the sidecar snapshot…',
  'sidebar.noSessions': 'No active sessions',
  'sidebar.noEvent': 'No events recorded yet',
  'sidebar.untitled': '(untitled)',
  'sidebar.boardHint': 'Full board: the "Sidecar" tab in the conversation view',
} satisfies Record<SidecarLocaleKey, string>
