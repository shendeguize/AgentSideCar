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
  'settings.liveEffectNote': 'Changes in this section take effect immediately after saving.',

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
  'settings.daemonProfileNote':
    'Read-only here: daemon lifecycle values come from the plugin row config in the profile cordis.patch. Settings changes do not drive the supervisor; edit the profile patch and restart DSH.',

  // ── sidecar invocation ─────────────────────────────────────────────────
  'settings.sectionSidecar': 'Sidecar invocation',
  'settings.sidecarCommandLabel': 'Executable command',
  'settings.sidecarCommandHint':
    'A PATH name, an absolute path, or a space-separated multi-part command (e.g. python3 /path/agent-sidecar.pyz); the plugin never installs the sidecar for you.',
  'settings.sidecarRuntimeDirLabel': 'Runtime directory',
  'settings.sidecarRuntimeDirHint':
    'Empty uses the default ~/.agent_sidecar (honoring AGENT_SIDECAR_RUNTIME_DIR); a non-empty value is passed to spawned daemons via the environment.',
  'settings.sidecarProfileNote':
    'Read-only here: sidecar command and runtime directory come from the plugin row config in the profile cordis.patch. Settings changes do not drive daemon invocation; edit the profile patch and restart DSH.',

  // ── stream reconciliation ──────────────────────────────────────────────
  'settings.sectionStream': 'Stream reconciliation',
  'settings.streamActiveMsLabel': 'Active cadence (ms)',
  'settings.streamActiveMsHint': 'Status snapshot cadence while any session is working.',
  'settings.streamIdleMsLabel': 'Idle cadence (ms)',
  'settings.streamIdleMsHint': 'Status snapshot cadence while no session is working.',
  'settings.streamProfileNote':
    'Read-only here: stream cadence values come from the plugin row config in the profile cordis.patch. Settings changes do not drive the reconciler; edit the profile patch and restart DSH.',

  // ── injection ──────────────────────────────────────────────────────────
  'settings.sectionInject': 'Message injection',
  'settings.injectEnabledLabel': 'Enable injection',
  'settings.injectEnabledHint':
    'When off, the inject panel renders read-only and disabled, and the server rejects write actions.',
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
  'settings.analysisProviderLabel': 'Analysis provider',
  'settings.analysisProviderHint':
    'Leave both route fields blank to use the host default; an explicit provider is used only when model is also set.',
  'settings.analysisProviderPlaceholder': 'Host default',
  'settings.analysisModelLabel': 'Analysis model',
  'settings.analysisModelHint':
    'Leave both route fields blank to use the host default; an explicit model is used only when provider is also set.',
  'settings.analysisModelPlaceholder': 'Host default',
  'settings.analysisRouteHostDefault':
    'Current route: host default model (provider and model are both blank).',
  'settings.analysisRouteExplicit': 'Current route: explicit {provider} / {model}.',
  'settings.analysisRoutePartial':
    'Provider and model must either both be blank or both be set. Complete the pair or clear both; Save is disabled.',

  // ── board UI ───────────────────────────────────────────────────────────
  'settings.sectionUi': 'Board UI',
  'settings.uiTimeWindowHoursLabel': 'Session time window (hours)',
  'settings.uiTimeWindowHoursHint': 'The board lists only sessions active within this window.',
  'settings.uiShowDeadLabel': 'Show dead sessions',
  'settings.uiShowDeadHint': 'Also list finished (dead) sessions on the board.',

  // ── auto archive ───────────────────────────────────────────────────────
  'settings.sectionArchive': 'Automatic archiving',
  'settings.archiveExplain':
    'Archiving affects this board only: no agent transcript is touched and no process is ended, and a session that becomes active again is unarchived automatically. The automatic path never disposes anything.',
  'settings.archiveLiveLabel': 'Policy in force',
  'settings.archiveLiveOn': 'on, threshold {hours}h',
  'settings.archiveLiveOff': 'off',
  'settings.archiveLiveUnknown': 'no daemon reached yet, or it predates archiving',
  'settings.archiveAutoLabel': 'Archive idle sessions automatically',
  'settings.archiveAutoHint':
    'Hide idle / dead sessions past the threshold from the board (off by default).',
  'settings.archiveAfterHoursLabel': 'Inactivity threshold (hours)',
  'settings.archiveAfterHoursHint':
    'Defaults to 24h — conservative on purpose, and safe in a loop with automatic unarchiving.',
  'settings.archiveHostedOnlyNote':
    'This setting applies only to daemons this plugin spawns, and takes effect at the next daemon start. An adopted or service-managed daemon keeps the policy it was started with; pass agent-sidecar daemon start --auto-archive there.',

  // ── skill mode ─────────────────────────────────────────────────────────
  'settings.sectionSkill': 'Skill mode',
  'settings.skillProvideLabel': 'Provide the skill in-process',
  'settings.skillProvideHint':
    'Provide the agent-sidecar skill to dsh via registerProvider (enabled in M4; applies after restart).',
  'settings.skillRestartNote':
    'skill.provide is read from the profile cordis.patch when the plugin loads. Edit the profile patch, then reload the plugin or restart DSH; Settings changes do not re-register the skill.',

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
  'inject.kimiTitle': 'Protected resume',
  'inject.kimiConfirmTitle': 'Confirm protected resume',
  'inject.kimiMessagePlaceholder':
    'Message for protected resume (16 KiB max; never paste secrets)',
  'inject.kimiActionLabel': 'Kimi action',
  'inject.kimiModeLabel': 'Protected resume',
  'inject.kimiModeHint':
    'Starts a separate Kimi ACP process and resumes the session through it. It never attaches to or steers the existing terminal.',
  'inject.kimiAuditNote':
    'This protected resume is recorded in the sidecar audit log (byte size and content fingerprint; never the message plaintext).',
  'inject.kimiPrepare': 'Prepare protected resume',
  'inject.kimiPreparing': 'Validating…',

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
  'inject.kimiConfirmExecute': 'Start protected resume',
  'inject.kimiExecuting': 'Starting protected resume…',
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
  'inject.kimiResultUnknown':
    'Kimi 0.38 completed this protected resume, but delivery to the resumed session cannot be proven. Do not automatically or manually retry the same content.',
  'inject.kimiResultFailed':
    'Rejected before the prompt was sent: no message was sent to Kimi.',
  'inject.kimiResultReplayed':
    'Safe replay: this is the cached result for the same request. No new Kimi ACP process was spawned and no content was sent again.',
  'inject.reprepare': 'Prepare again',
  'inject.observeListen': 'Listen for the reaction',
  'inject.verifying': 'Checking the target transcript for the message…',
  'inject.verifyConfirmed':
    'Found it: the message is in the target transcript, so it was delivered. Still do not send it again.',
  'inject.verifyAbsent':
    'Not found in the target transcript. It most likely never arrived — confirm in the session yourself before sending anything again.',
  'inject.verifyUnavailable':
    'The transcript could not be read, so nothing was learned; open the target session to check by hand.',
  'inject.openTarget': "Show the target session's timeline",

  // ── inject panel: error vocabulary (gateway + transport) ─────────────
  'inject.errInjectDisabled': 'Injection is disabled on the server; enable it in Settings.',
  'inject.errInvalidMessage': 'The message failed server-side validation.',
  'inject.errTargetNotFound':
    'The target session does not exist or left the observation window.',
  'inject.errTargetDead': 'The target session has ended (dead); it cannot be injected.',
  'inject.errWorkingSession':
    'This external-agent session is working; injection is available only while it is waiting or idle.',
  'inject.kimiErrWorkingSession':
    'This Kimi session is working; protected resume is available only while it is waiting or idle.',
  'inject.errChildSession':
    'External child or sidechain sessions cannot be injected from Agent Sidecar.',
  'inject.errRemoteSession':
    'Remote sessions cannot be injected from this local host.',
  'inject.errInvalidSession':
    'The host did not provide a valid target eligibility verdict; injection stays disabled. Update or restart the host and reopen this session.',
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
  'inject.errUnknown': 'The request failed for an unknown reason.',
  'inject.errGeneric': 'Request failed ({code}).',

  // ── board tab chrome + session board ──────────────────────────────────
  'board.viewBoard': 'Session board',
  'board.viewProjects': 'Projects',
  'board.status.working': 'Working',
  'board.status.waiting': 'Waiting',
  'board.status.idle': 'Idle',
  'board.status.dead': 'Finished',
  'board.status.unknown': 'Unknown',
  'board.attention.gap': 'Event gap',
  'board.daemon.probe': 'Probing',
  'board.daemon.adopted': 'Connected · adopted',
  'board.daemon.defer': 'Waiting for the system service',
  'board.daemon.reprobe': 'Re-probing',
  'board.daemon.hosting': 'Starting',
  'board.daemon.hosted': 'Connected · hosted',
  'board.daemon.backoff': 'Restart backoff',
  'board.daemon.failed': 'Offline',
  'board.stream.ok': 'Live stream healthy',
  'board.stream.degraded': 'Live stream reconnecting',
  'board.stream.unknown': 'Live stream not connected',
  'board.banner.daemonFailed':
    'sidecar is offline: the board shows the last snapshot and will not update',
  'board.banner.daemonStale':
    'The daemon is still running old code (running v{running}, installed v{installed}); this page reflects the old version until the daemon restarts',
  'board.banner.daemonStaleCode':
    'The installed code changed after the daemon started (still v{running}); this page reflects the code it loaded until the daemon restarts',
  'board.banner.streamDegraded': 'The live stream is reconnecting; data may be stale',
  'board.empty.daemonFailedTitle': 'sidecar is offline',
  'board.empty.daemonFailedHint':
    'Repeated daemon start failures tripped the circuit breaker. Retry from Settings, or run agent-sidecar daemon start and wait for automatic adoption.',
  'board.empty.daemonDeferTitle': 'Waiting for the system service to start the daemon',
  'board.empty.daemonDeferHint':
    'A LaunchAgent manages the daemon, so the plugin will not start it. The board populates automatically once the service starts it.',
  'board.empty.filteredTitle': 'No sessions match these filters',
  'board.empty.filteredHint':
    'Try a wider time window, clear the status filter, or enable "Show finished".',
  'board.empty.noSessionsTitle': 'No observed sessions yet',
  'board.empty.noSessionsHint':
    'Local agents (claude / codex / cursor / dsh …) appear here automatically once they start working.',
  'board.topbar.title': 'Sidecar multi-agent board',
  'board.topbar.refresh': 'Refresh',
  'board.topbar.refreshing': 'Refreshing…',
  'board.topbar.refreshTitle': 'Fetch the latest snapshot',
  'board.topbar.refreshFailed': 'Refresh failed; the board is still showing the previous snapshot',
  'board.topbar.dismiss': 'Dismiss',
  'board.topbar.showDead': 'Show finished',
  'board.topbar.collapseIdle': 'Fold idle',
  'board.topbar.collapseIdleTitle': 'Group idle sessions by project',
  'board.topbar.analyzeCrossAgent': 'Cross-agent analysis',
  'board.topbar.cluster': 'Cluster sessions',
  'board.topbar.timeWindow': 'Time window',
  'board.topbar.agentFilter': 'Agent',
  'board.topbar.agentFilterAria': 'Filter sessions by agent type',
  'board.topbar.allAgents': 'All agents',
  'board.topbar.countWorking': '{n} working',
  'board.topbar.countWaiting': '{n} waiting',
  'board.topbar.countTotal': '{n} sessions total',
  'board.topbar.filterByStatusTitle': 'Show only sessions marked "{label}"',
  'board.topbar.clearStatusFilterTitle': 'Clear the status filter and show all sessions',
  'board.states.loading': 'Loading the first sidecar snapshot…',
  'board.group.collapseTitle': 'Collapse this group',
  'board.group.expandTitle': 'Expand this group',
  'board.group.showAll': 'Show all {n} sessions',
  'board.group.showLess': 'Show first {n} only',
  'board.group.idleSummary': '{n} idle sessions',
  'board.group.expandIdle': 'Expand idle sessions',
  'board.group.collapseIdle': 'Fold idle sessions',
  'board.card.noEvent': 'No events yet',
  'board.card.untitled': '(untitled)',
  'board.card.observedDisclaimer':
    'Status is an observed value inferred from persisted data and may lag',
  'board.card.observedValue': 'Observed value: {status}',
  'board.card.lastReconcile': 'Last reconciled: {time}',
  'board.card.neverReconciled': 'Not reconciled yet',
  'board.card.copyId': 'Click to copy the full session ID',
  'board.card.copied': 'Copied',
  'board.time.justNow': 'just now',
  'board.time.minutesAgo': '{n} min ago',
  'board.time.hoursAgo': '{n} h ago',
  'board.timeWindow.hours': '{n} hours',
  'board.timeWindow.days': '{n} days',
  'board.groupCount': '{n} sessions',
  'board.unknownProject': 'Unknown project',
  'board.cluster.title': 'Pod-local deterministic clusters',
  'board.cluster.count': '{n} clusters',
  'board.cluster.empty': 'No sessions are available to cluster',
  'board.cluster.sessions': '{n} sessions',
  'board.archive.open': 'Batch archive',
  'board.archive.openTitle': 'Archive idle / dead sessions by inactivity threshold',
  'board.archive.title': 'Archive idle sessions',
  'board.archive.explain':
    'Archiving only affects this board: it never edits an agent session file and never stops a process. A session that becomes active again returns automatically.',
  'board.archive.threshold': 'Inactive longer than',
  'board.archive.threshold30m': '30 minutes',
  'board.archive.threshold2h': '2 hours',
  'board.archive.threshold24h': '24 hours',
  'board.archive.thresholdCustom': 'Custom (minutes)',
  'board.archive.customMinutes': 'minutes',
  'board.archive.preview': 'Preview',
  'board.archive.previewing': 'Previewing…',
  'board.archive.previewEmpty': 'No session matches this threshold',
  'board.archive.previewCount': '{n} matched, {selected} selected',
  'board.archive.selectAll': 'Select all',
  'board.archive.selectNone': 'Select none',
  'board.archive.confirm': 'Archive {n} selected',
  'board.archive.confirming': 'Archiving…',
  'board.archive.cancel': 'Cancel',
  'board.archive.done': 'Archived {n} sessions',
  'board.archive.failed': 'Archive failed: {reason}',
  'board.archive.previewFailed': 'Preview failed: {reason}',
  'board.archive.unavailable': 'This daemon has no archive support; upgrade agent-sidecar',
  'board.archive.dispose': 'Also end the {n} DSH session(s)',
  'board.archive.disposeHint':
    'dsh sessions only; this really ends the session and cannot be undone',
  'board.archive.doneDisposed': ', {n} DSH session(s) ended',
  'board.archive.doneDisposeFailed': ', {n} could not be ended (still archived)',
  'board.archived.summary': '{n} archived',
  'board.archived.expand': 'Show archived sessions',
  'board.archived.collapse': 'Hide archived sessions',
  'board.archived.reason.manual': 'manual',
  'board.archived.reason.batch': 'batch',
  'board.archived.reason.auto': 'auto',
  'board.archived.archivedAt': 'Archived {time}',
  'board.archived.restore': 'Restore',
  'board.archived.restoring': 'Restoring…',
  'board.archived.restoreAll': 'Restore all',
  'board.archived.restoreFailed': 'Restore failed: {reason}',
  'board.widget.label': 'Sidecar',
  'board.widget.connection.ok': 'Connected',
  'board.widget.connection.degraded': 'Connection unstable',
  'board.widget.connection.off': 'Offline',
  'board.widget.working': '{n} sessions working',

  // ── session detail ───────────────────────────────────────────────────
  'detail.header.close': 'Back to board',
  'detail.header.listenOn': 'Listening',
  'detail.header.listenOff': 'Listen',
  'detail.header.listenHint': 'New events append live and get highlighted while on',
  'detail.header.refresh': 'Refresh',
  'detail.header.refreshing': 'Refreshing…',
  'detail.header.refreshHint': 'Pull the newest timeline window',
  'detail.header.copyIdTitle': 'Click to copy the session ID',
  'detail.header.copied': 'Copied',
  'detail.header.untitled': '(untitled)',
  'detail.header.unknownProject': 'Unknown project',
  'detail.header.observedDisclaimer':
    'Status is an observed value inferred from persisted data and may lag',
  'detail.header.lastActivity': 'Last activity {time}',
  'detail.header.duration': 'Session span {span}',
  'detail.header.durationUnderMinute': 'under a minute',
  'detail.header.durationMinutes': '{m}m',
  'detail.header.durationHours': '{h}h {m}m',
  'detail.header.durationDays': '{d}d {h}h',
  'detail.header.model': 'Model {name}',
  'detail.header.transcript': 'Transcript',
  'detail.header.copyPathTitle': 'Click to copy the full path',
  'detail.header.copyProjectTitle': 'Click to copy the working directory',
  'detail.header.loadedEvents': '{n} events loaded',
  'detail.header.loadedEventsPartial': '{n} events loaded (older history remains)',
  'detail.header.kindCount': '{label} {n}',
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
  'detail.sources.healthSummary':
    '{available} available · {unavailable} unavailable · {failed} failed',
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
  'detail.filter.conversation': 'Conversation only',
  'detail.filter.all': 'All events',
  'detail.filter.hiddenNotice': '{n} protocol events hidden',
  'detail.timeline.loadMore': 'Load older history',
  'detail.timeline.loadingMore': 'Loading…',
  'detail.timeline.noMore': 'Start of the timeline',
  'detail.timeline.expand': 'Expand',
  'detail.timeline.collapse': 'Collapse',
  'detail.timeline.newBadge': 'New',
  'detail.timeline.seq': 'seq {n}',
  'detail.timeline.hiddenNotice': '{n} earlier entries collapsed to stay smooth',
  'detail.timeline.showAll': 'Show all',
  'detail.timeline.chunkRun': '{n} streaming chunks',
  'detail.timeline.degradedPartial':
    'Some timeline sources are unavailable. The events shown may be incomplete.',
  'detail.timeline.degradedAll':
    'All usable timeline sources failed for the latest request. No new events could be loaded.',
  'detail.timeline.degradedUnverified':
    'Timeline source status could not be verified. Events may be incomplete.',
  'detail.timeline.degradedRetry': 'Use Refresh to try the timeline sources again.',
  'detail.timeline.volatileOnly':
    'These events come only from the live buffer. No durable source answered, so older history may not be among them.',
  'detail.timeline.volatileOnlyHint':
    'If this session should have readable history, check that the sidecar daemon is running and current, then press Refresh.',
  'detail.timeline.daemonDownHint':
    'The sidecar daemon is not running, so nothing can answer for history. Retry from the settings card, or run agent-sidecar daemon start and press Refresh.',
  'detail.timeline.daemonDeferHint':
    'The plugin defers to a service-managed daemon; once the service is up, press Refresh to load history.',
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
  'detail.actions.dispose': 'End session',
  'detail.actions.disposeHint': 'Really end this DSH session; cannot be undone',
  'detail.dispose.title': 'End this DSH session',
  'detail.dispose.explain':
    'This really ends the session: unlike archiving it cannot be undone, and the session process and context are released. The persisted history stays on disk.',
  'detail.dispose.confirm': 'End the session',
  'detail.dispose.disposing': 'Ending…',
  'detail.dispose.cancel': 'Cancel',
  'detail.dispose.outcome.unsupported': 'This DSH host cannot end sessions',
  'detail.dispose.outcome.timeout':
    'The request timed out; the session may still be running — refresh to check',
  'detail.dispose.outcome.failed': 'Could not end the session; it is still running',
  'detail.tools.title': 'Lineage & search',
  'detail.tools.show': 'Show',
  'detail.tools.hide': 'Hide',

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
  'project.analyze': 'Analyze this project',
  'project.liveChip': 'Live',
  'project.untitled': '(untitled)',
  'project.showAllSessions': 'Show all {n} sessions',
  'project.showLessSessions': 'Show first {n} only',
  'project.empty.title': 'No project correlation yet',
  'project.empty.hint':
    'No cross-agent project activity within the time window; projects appear here once an agent works inside a project directory.',
  'project.loading': 'Loading project correlation…',
  'project.errorTitle': 'Project correlation failed to load',

  // ── AI bypass analysis panel ────────────────────────────────────────────
  'analysis.title': 'AI analysis',
  'analysis.back': 'Back',
  'analysis.close': 'Close',
  'analysis.disabledNote':
    'AI bypass analysis is off; enable "AI bypass analysis" in Settings first.',
  'analysis.idleHint':
    'Spin up one dsh bypass-analysis pass over this session on demand (consumes model tokens).',
  'analysis.start': 'Start analysis',
  'analysis.requesting': 'Analyzing… (up to ~60 s)',
  'analysis.exchangeInitial': 'Analysis summary',
  'analysis.followupLabel': 'Follow-up',
  'analysis.userLabel': 'User',
  'analysis.assistantLabel': 'Assistant',
  'analysis.streamingSegment': 'Analysis in progress… segment {n}',
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
  'analysis.errTimeout':
    'The analysis timed out before the model began answering; its session was released, so start again.',
  'analysis.errTimeoutPartial':
    'The analysis timed out. Above is what the model produced before the deadline; its session was released, so start again for a complete answer.',
  'analysis.errTimeoutCreate':
    'Creating the analysis session timed out, so no analysis ran. This usually means the model service is busy or unconfigured; try again shortly.',
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
  'command.truncated': '{n} more sessions not listed',
  'command.boardHint': 'Open Agent Center',
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

  // ── sidebar navigation (plain DOM + optional better-sidebar) ───────────
  'sidebar.centerEntryLabel': 'Agent Center',
  'sidebar.centerEntryAria': 'Open Agent Center',
  'sidebar.tabTitle': 'Sidecar',
  'sidebar.countsRow': '{working} working · {waiting} waiting',
  'sidebar.recentTitle': 'Recently active',
  'sidebar.connecting': 'Waiting for the sidecar snapshot…',
  'sidebar.noSessions': 'No active sessions',
  'sidebar.noEvent': 'No events recorded yet',
  'sidebar.untitled': '(untitled)',
  'sidebar.boardHint': 'Full board: the "Sidecar" tab in the conversation view',
} satisfies Record<SidecarLocaleKey, string>
