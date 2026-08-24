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
} satisfies Record<SidecarLocaleKey, string>
