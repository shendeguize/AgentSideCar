# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.2] - 2026-09-03

### Fixed

- A daemon that was still building its first index no longer reads as a dead
  or absent one. It binds its socket and records its pid before that scan and
  answers nothing until the scan ends, which on a 1,950-session machine took
  up to 22 seconds; every command that met that silence called it absence.
  `daemon start` waited 5 seconds and, when a service claimed the runtime in
  the gap after a `stop`, reported `daemon child exited before readiness`
  while the winner was seconds from serving. It now waits out a live owner —
  up to 45 seconds, ending the moment a ping lands — and adopts it, while a
  child that died with nothing holding the runtime still fails immediately.
  `daemon status` and `daemon stop` name the live owner instead of declaring
  `daemon is not running`, and `service status` reports the pid launchd or
  systemd just handed back rather than contradicting it.
- The plugin's supervisor no longer executes the daemon it just spawned for
  taking too long to index. Its readiness window was 5 seconds against a
  first scan that runs 22 seconds on a 1,950-session machine, so hosting
  killed each healthy daemon mid-scan, backed off, and respawned into the
  same fate until the failure budget tripped. Installs without a service
  therefore never got a daemon at all on a large index. The window is now 45
  seconds and still ends the moment the daemon answers.
- A remote `watch` child that took longer than a second to reap turned an
  orderly shutdown into a crash. The bootstrap's cleanup ran two one-second
  waits and the second one was unguarded, so on a loaded host the payload
  exited 1 with a `TimeoutExpired` traceback instead of reporting the status
  it had determined. Both waits now tolerate a slow child; the group kill has
  already gone out either way.

## [0.11.1] - 2026-09-03

### Changed

- The analysis digest is smaller and its bounds now agree with each other.
  A session summary carries the newest 24 timeline entries (was 120), lines
  clamp at 120 chars to match the event text the daemon already snips, the
  question clamps at 800, and both the assembly and the engine cap the whole
  input at 6000 chars (was 8000 each). The item caps are sized so their worst
  case fits the char cap, which makes that cap a backstop rather than the
  thing that routinely decides what the model sees.

### Fixed

- A session digest that could not fit its whole timeline said nothing about
  it. The char cap silently cut the oldest entries — for a long session most
  of them — while the header still announced the full event count and the
  prompt still called itself un-truncated. The digest now states that older
  events exist beyond the window it shows.

## [0.11.0] - 2026-09-03

### Fixed

- A session that simply does not exist in a timeline source no longer counts
  as a broken source. `dsh-session-query` answers `SESSION_NOT_FOUND` for
  every non-DSH session, and that namespaced code was unrecognized, so any
  claude/codex/copilot/cursor/kimi detail page opened with a "sources failed"
  warning over a complete timeline.
- The AI analysis budget no longer charges session creation to the first model
  turn. Creation and the turn each get their own deadline, a timeout records
  which stage ran out, and text the model had already produced is kept and
  shown instead of being discarded with the turn.

### Added

- A detail page whose history came only from the in-memory ring buffer now
  says so, and stops claiming the visible events reach the start of the
  timeline. Correctly recognizing an absent session (above) would otherwise
  have silenced the one warning that hinted at genuinely truncated history.
- The daemon now reports drift against its own installed code, and both
  `daemon status` and the board say so. A daemon that outlives an upgrade
  keeps answering pings normally, so nothing used to contradict a board built
  from code that was replaced hours ago. It reports the version sitting in its
  source tree — the number `daemon status` now compares against, rather than
  the CLI's own version, which is only a stand-in and wrong when the two come
  from different installs — plus a content fingerprint of that tree, because
  between releases the version cannot move at all and a same-version daemon
  can still be many edits behind. Content, not timestamps: a rewrite with
  identical bytes is not drift, and a false stale warning would teach
  operators to ignore the real one.

## [0.10.0] - 2026-09-01

### Fixed

- Remote cluster rows no longer carry the remote's own `local` label. Every
  host groups its sessions under the reserved `local` alias, so the fleet view
  used to claim each remote group also existed on the workstation — including
  single-session groups. Remote rows are now attributed to their actual host.
- `agent-sidecar daemon status` reports the running daemon's version and warns
  when it differs from the CLI. A long-lived daemon keeps serving the code it
  started with, so an upgrade would otherwise silently return stale sessions.
- Sessions from non-DSH agents no longer open with an empty timeline. The
  daemon now replays the on-disk transcript for every adapter, so a
  claude/codex/copilot/cursor/kimi session shows its history immediately
  instead of only the events that happen to arrive after the page is opened.
  A transcript an adapter cannot replay reports `replay_unsupported` and is
  presented as unavailable rather than as a failure.
- The DSH plugin's session detail page keeps its header, and therefore its
  Back button, when the timeline is unavailable. The degraded state used to
  replace the whole page, leaving no way back to the board except browser
  navigation.
- The board's model column was always blank: the session projection dropped
  `model` on the way to the view.

### Added

- `agent-sidecar service install|status|uninstall` now supports an explicit
  Linux current-user systemd unit. The unit runs the foreground daemon with
  bounded restart, process-group cleanup, least-privilege hardening, and
  journald output; it never changes system-wide service state or user lingering.
- Linux `send` now has process-group containment for bounded child execution,
  allowing supported local injection paths to run on Linux while retaining
  fail-closed cleanup and timeout behavior.
- Kimi Code 0.39.1 is supported by the protected ACP spawn-resume path.
- Copilot CLI injection is supported through `--resume --interactive` for
  authenticated, eligible sessions, including the DSH plugin's send-cli route.
- `scripts/deploy-to-pod.sh` provides an explicit operator path for rsyncing
  the zipapp and plugin, restarting the daemon, and installing the DSH plugin.
- `scripts/copilot_compat.py` provides a bounded, no-credential Copilot CLI
  compatibility smoke for the authenticated `--resume --interactive` flag
  contract.
- The plugin adds a line-network Agent Center sidebar icon, a persisted
  per-project idle-session fold, project/cross-agent analysis entry points,
  and a multi-turn analysis conversation with an honest segmented progress
  fallback.
- `agent-sidecar archive`, `archive list`, `unarchive`, and `list --archived`
  hide idle or dead sessions from every listing without touching them. Nothing
  is edited and no process is signalled: the only state written is a private
  `archive.json` registry, and a session that becomes active again is released
  automatically. `daemon start --auto-archive [--auto-archive-after 24h]` runs
  the same selection on a timer, off by default and archive-only. Archiving is
  per host; `--remote`/`--host` are refused with the equivalent ssh
  invocation.
- The plugin board can archive in bulk: pick an inactivity threshold, review
  and deselect the matched sessions, then confirm. Archived sessions move to a
  collapsible "archived N" section that can release them again.
- DSH sessions can be ended from the plugin. The detail page offers an
  explicit, confirmed end-session action, and the batch archive dialog offers
  it as an opt-in checkbox for the DSH sessions in the selection. It is the
  one destructive action the plugin exposes, so it is DSH-only, manual, and
  behind the same write gate as injection.
- The session detail header reports last activity, session duration, event
  counts, model, and copyable project and transcript paths.
- An injection whose delivery is reported as unknown is now checked
  automatically: the plugin reads the target's transcript a bounded number of
  times and reports whether the message actually landed. It never re-sends,
  and it distinguishes "not in the transcript" from "the transcript could not
  be read".

### Changed

- Dedicated analysis sessions are explicitly marked and filtered out of
  board and project correlation data.
- The DSH plugin's send-cli execution timeout is now 180 seconds to cover
  bounded real-agent resume operations.
- Copilot documentation now distinguishes stdin isolation at the Sidecar
  boundary from the upstream Copilot child process's argv message transport.
- The plugin package uses its Trusted Publishing release workflow for npm
  publication.
- Added bounded deterministic session clustering with local and remote fleet
  sources, model/provider metadata, and optional redacted DSH headless
  enrichment.
- Cursor observation now includes Remote-SSH `cursor-server` history manifests
  and marks their provenance explicitly.
- Every adapter now reports session creation time as `extra.created_at_epoch`
  in seconds, so age and duration read the same key regardless of agent.

## [0.9.0] - 2026-08-29

### Fixed

- Cursor CLI WAL snapshots are normalized only in their private temporary copy
  before read-only decoding, so cleanly exited stores remain readable without
  modifying the source database. Snapshot normalization/open failures now use
  the distinct `CursorChatOpenError` diagnostic.
- Remote observation now includes eligible DSH Center hosts in `running` and
  `degraded` phases. Explicit selection of an unknown or ineligible host exits
  with the existing control error and does not emit a misleading local snapshot.
- Native send receipts now identify the bounded output stream and limit when
  overflow occurs, even if cleanup containment is also incomplete. A successful
  Darwin native exit with an observed fork is reported as completed with
  unknown delivery and `cleanup_incomplete`, rather than as a native failure.
- DSH cold injection now gates presets from the target's persisted state rather
  than the host's live `agentPresets` service, and plugin failures include
  bounded sanitized detail with a distinct unavailable-agents code.
- Core coverage enforcement now requires at least 97% line and branch coverage,
  compares results with the committed v0.9.0 baseline, and rejects unapproved
  suppression growth.
- Added a versioned functional/design traceability matrix and a standard-library
  selector that reports mapped Python and plugin suites plus unmapped release
  items during migration.
- The local and weekly quality gates now use the fast path, which executes the
  full test suite once under coverage instead of repeating it as a separate
  test stage.

## [0.8.0] - 2026-08-28

### Added

- Added explicit `audit rebind` recovery for strictly verified moved
  namespaces, preserving the original epoch and request-ID idempotency history.
- Added `audit reset --purge` for deliberately deleting active and archived
  audit state with a separate confirmation.
- Added a versioned synthetic DSH Center inventory fixture and contract-test
  coverage for eligible hosts, strict filtering, aliases, bounded input, and
  both array and container payload forms.

### Changed

- `audit reset` now archives the active key, marker, and retained logs by
  default, refuses after eight archives, and reports `namespace_moved` as a
  safe recovery detail on `audit_corrupt`.
- `send` now adds a human-readable hint on `target_not_found` explaining that
  rows from `list --remote` cannot be targeted by `send`.

### Documentation

- Documented the DSH Center C1–C4 remote inventory contract in the English and
  Chinese READMEs and agent reference, including the read-only boundary and
  cross-repository issue workflow.
- Added a DSH Center integration issue template with redaction guidance.
- Documented the `send` boundary between `target_not_found` for rows absent
  from the direct local scan and the `remote_session` rejection for locally
  scanned rows with remote provenance, across both READMEs, the skill, and the
  agent reference.

## [0.7.0] - 2026-08-27

### Added

- Remote `list`, `status`, and `watch` now accept
  `--remote-python <absolute-path>` and `AGENT_SIDECAR_REMOTE_PYTHON` as a
  fleet-wide interpreter pin, with CLI-over-environment precedence, local
  validation, and fail-closed per-host diagnostics without silent fallback.
  Bootstrap preserves that explicit operator token verbatim; a differing
  validated executable in the probe response cannot replace it.

### Changed

- Remote observation now accepts Python 3.8+ on SSH targets instead of 3.9+;
  local installation and tooling continue to require Python 3.9+.
- Remote observation now probes the bounded candidate order `python3`, then
  `python3.14` through `python3.8`, and uses the first available interpreter
  meeting the Python 3.8+ target floor. In bounded default discovery, the probe
  reports the resolved executable path, which is reused only by the bootstrap
  in the same host session; every invocation probes afresh without local or
  remote persistence. Exhausting the candidates retains the existing
  `python_too_old` code.

## [0.6.0] - 2026-08-26

### Added

- A first-class dsh Agent Center (plugin 0.3.0) through the official `shell.overlay`
  registry, shared by the main sidebar, footer widget, and `/sidecar`, plus a
  public theming contract for host tokens, stable parts, and `--agsc-*`
  overrides. The conversation tab and optional better-sidebar summary remain
  secondary surfaces.
- An agent-type filter on the Agent Center session board, composed with the
  existing status, time-window, and dead-session filters.
- Optional `send --agent NAME --exact-session` selectors for an exact
  agent/session composite binding. The DSH plugin always uses both selectors
  for external injection and treats a mismatched agent, session, or request ID
  in the CLI receipt as delivery-unknown `executor_error`.
- Exact-version Kimi Code 0.38.0 protected ACP spawn-resume for local top-level
  `waiting`/`idle` sessions. It binds Kimi home/project/root-wire identity and
  a plan-private package, Node, and non-system dylib snapshot; reprobes version
  and ACP initialization; guards project owner processes; runs Kimi's default
  mode with no MCP/filesystem/terminal capabilities; cancels permission
  requests and fail-closes question/approval paths; and carries the prompt in
  ACP rather than argv.

### Changed

- Reworked the browser surfaces around DSH UI primitives, semantic host theme
  tokens, accessible contrast, and the host English/Chinese locale service.
  Real `dsh_web` acceptance passed light, dark, and responsive layouts, shared
  sidebar/footer overlay access, and nested focus and inert behavior, with no
  console or network errors; locale switching remains covered by automated
  tests rather than this browser pass.
- The Settings card now presents `analysis.provider` and `analysis.model` as
  one paired route: both blank selects the host default, both populated selects
  an explicit route, and a partial pair cannot be saved. The non-live
  daemon/sidecar/stream groups are read-only there and direct operators to the
  profile `cordis.patch` plus a DSH restart. `/sidecar` now labels its
  navigation action as Agent Center.
- Injection availability is now a host-derived per-session contract, separate
  from the global `inject.enabled` gate. Unsupported agents are visibly
  disabled; external injection is limited to local top-level
  Claude/Codex/Cursor CLI/Kimi sessions in `waiting` or `idle`; local DSH
  `working` sessions support in-process steering and persisted DSH
  `waiting`/`idle`/cold sessions proceed through no-preset live/cold preflight
  with the host's complete current model route. Remote, dead, malformed, and
  unsupported targets fail closed with localized, accessible reasons.
- Kimi injection is a manual protected resume, never live inbox queue/steer.
  An rc-0 settled turn with strict durable wire/state completion proof is
  intentionally reported as `outcome: completed`, `delivery: unknown`, and
  CLI exit `1`, including when Darwin fork containment remains diagnostically
  `cleanup_incomplete`; without proof it remains failed and unknown. The same
  retained request ID only replays the audit result, while blind retries under
  a new ID are forbidden. Claude, Codex, and Cursor retain their prior send
  semantics, and direct CLI send remains unsupported for DSH.
- The Kimi owner guard now boundedly parses canonical Node file arguments,
  including supported option values and later script arguments. Ordinary long
  Node commands and, under the owner-safe single-link executable boundary,
  ordinary relative Node commands with a deleted cwd no longer become global
  blockers; Kimi hints, identity matches, malformed/over-budget input carrying
  that evidence, and unsafe executable identity still fail closed.
- Timeline pages now expose canonical merged entries plus content-free
  `sourceOutcomes`, `degraded`, and `reason` fields. Same-sequence sibling
  blocks survive deduplication and pagination; partial source failures retain
  available events with a warning, while total source failure is reported
  explicitly. Late-bound DSH services are re-resolved on use, and
  generation-scoped detail, pagination, refresh, and listen requests prevent
  stale settlements from rolling back entries, health, metadata, or cursors.

### Fixed

- The board now exposes localized first-snapshot loading with `aria-busy`,
  settles a failed initial load into a retryable error, and preserves the last
  successful snapshot when a later refresh or stream update fails.
- Board/project-to-detail navigation now moves focus into detail, remembers
  source rows by the composite agent/session identity, restores each view's
  clamped scroll offset, and returns focus to the exact source or its heading
  fallback. Detail-internal session jumps deliberately use that fallback.
- Nested host modals now make every lower dialog both `inert` and
  `aria-hidden`, then restore prior focus and the exact pre-existing host
  attributes as layers close, unload, or hand over across reloads. Ownership
  tracking now drains queued records at HMR handoff and handles nested
  remove/reinsert races without restoring a stale opener.
- Real DSH end-to-end testing exposed and fixed headless discovery gaps.
  Discovery now follows an independent absolute `DSH_HOME` and boundedly falls
  back to validated durable session headers, so headless sessions remain
  visible when no Web projection-cache row exists. Durable fallback now drains
  fast-exit decoder output, binds no-follow regular-file descriptors before
  decoding, detects duplicate identities before its candidate cutoff, enforces
  one total deadline, reuses one fixed-identity `zstd` binding per adapter
  (a Linux descriptor or private macOS descriptor snapshot) with explicit and
  finalizer cleanup, and caches deterministic headers by
  device/inode/size/mtime/ctime without suppressing retries after transient
  decoder failures or decoding projection-cache-covered sessions. Binding
  failure degrades to projection-cache-only discovery. The bounded
  per-candidate decoder timeout is 3.5 seconds within the five-second
  whole-scan hard deadline.
- Real DSH end-to-end testing also exposed and fixed the cold injection
  route/preset lifecycle. Cold injection now resumes with the host's current
  complete default provider/model route; `inject.prepare` rejects a missing or
  partial route with HTTP 409 `dsh_model_unconfigured` and no confirm token.
  A stale board target that authoritative persistence proves missing returns
  HTTP 404 `target_not_found`; a proved effective preset remains unsupported
  and returns HTTP 409 `dsh_preset_unsupported`; unavailable, failed, or
  indeterminate cold services return HTTP 502 `executor_error`. Already-live
  Agent injection remains unaffected. Timeouts and host-service reload races
  fail closed without exposing route or preset values. For in-process DSH
  injection, `delivered` means only that the DSH inbox accepted/queued the
  message, not that a model turn succeeded.
- Final content-free real acceptance recorded Kimi protected-resume PASS and
  DSH live-steer/UI PASS. No private path, session identifier, transcript,
  prompt, or hash is included.

### Security

- Sanitized Node and macOS dyld injection variables from every bound Kimi child
  environment while preserving normal user, Kimi home, and locale settings.
- Documented the exact Kimi filesystem/runtime/process checks without claiming
  cryptographic request-to-provider-turn binding. Kimi advertises no MCP,
  filesystem, or terminal capability, and its message is absent from both the
  Sidecar and native Kimi argv.
- Persistently bound runtime audit namespaces must not be deleted, recreated,
  or replaced to bypass `unsafe_lock`; that error means retained history or
  identity cannot be proved. A new owner-private runtime is only for an
  explicitly new request lineage and must retain its own audit history.
- Hardened the DSH plugin's current cleartext loopback guard: a supplied
  `Origin` must be the matching HTTP host/port tuple, HTTPS origins are
  rejected, and duplicate `Host` or `Origin` fields fail closed even when
  values match. A single valid loopback `Host` remains accepted on any valid
  port.
- Clarified that the local owner-facing plugin UI may display full project
  paths, while screenshots, JSON, logs, and other evidence shared outside that
  local UI must redact project paths and other stable identifiers.

## [0.5.0] - 2026-08-25

### Changed

- The dsh plugin board and detail views got a UX overhaul (plugin 0.1.1):
  board header status counts with one-click filtering, collapsible groups
  with bounded card rendering, a conversation-first timeline filter that
  aggregates streaming chunks, newest-first detail positioning, a
  post-injection observe loop (auto-refresh plus a listen shortcut),
  keyboard support (Esc, Cmd+Enter, autofocus), copyable session ids, and
  absolute short timestamps.

### Added

- A native dsh plugin M1 monitoring milestone in the `plugin/` npm
  sub-package: a probe-adopt-else-host daemon supervisor, a Unix-socket
  bridge with snapshot reconciliation, plugin API routes with SSE streaming
  behind a five-layer loopback guard, a cross-agent monitoring board, and a
  settings namespace with its settings card.
- The dsh plugin M2 injection milestone: two-phase, confirm-token message
  injection behind the default-off `inject.enabled` gate, with in-process
  queue/steer delivery for dsh sessions, `send --message-stdin` delivery for
  external agents, and a read-only `/sidecar` slash-command overview.
- The dsh plugin M3 fusion milestone: paged session-detail timelines, dsh
  lineage and full-text search with honest degradation, project grouping,
  and default-off AI bypass analysis in bounded dedicated sessions with
  optional `analysis.provider`/`analysis.model` model routing.
- An embedded agent-sidecar skill provider in the dsh plugin
  (`skill.provide`, default on; a filesystem-installed skill of the same
  name automatically wins) and an optional better-sidebar monitor tab.
- A dsh target in the checkout skill installer: `scripts/install-skill.sh`
  now also links `~/.dsh/skills/agent-sidecar`, and the skill documents
  dsh-specific guidance that routes dsh-session injection through the dsh
  plugin because `send` does not support dsh sessions.
- A `send --message-stdin` option that reads the message from standard input
  instead of the positional argument, keeping it out of the `agent-sidecar`
  command line while reusing the same validation, injection pipeline, audit
  identity, receipts, and exit codes. The two message sources are mutually
  exclusive. Interactive terminals are refused with a usage error instead of
  blocking, unreadable standard input reports a dedicated diagnostic, and
  interrupting the read exits `130` cleanly before any delivery.
- A daemon protocol `replay` operation that returns one bounded page of a
  session's transcript-retained events after a `seq` cursor, with `limit`,
  `last_seq`, and `truncated` paging semantics. It is backed by the session
  adapter's bounded local-transcript replay, which currently only `dsh`
  sessions provide; other agents report `replay_unsupported` and unknown
  sessions report `unknown_session`.
- An optional `agents` allowlist on the daemon `subscribe` operation for
  server-side filtered event streams; filtered-out events never consume the
  subscriber's bounded queue. `SidecarClient` gains a paging `replay(...)`
  method and a `subscribe(agents=...)` parameter. Requests without the new
  fields keep the existing full-stream behavior, so old clients are
  unaffected.

## [0.4.4] - 2026-08-24

### Fixed

- Made Codex immutable status fallback copy only an unchanged regular main
  database into a private snapshot, so FIFO replacement and source races
  return promptly without opening the live source through SQLite.
- Recovered aged ownerless installer operation locks and safely cleaned
  interrupted recovery artifacts without stealing fresh or live locks.

### Security

- Closed status-read FIFO blocking and pathname TOCTOU windows by binding
  bounded copies to descriptor-verified regular-file identity and opening
  SQLite only on private snapshots.
- Bound installer stale-lock recovery to process identities and per-operation
  tokens with serialized, inode-checked recovery gates while retaining
  fail-closed handling for unsafe paths.

## [0.4.3] - 2026-08-24

### Fixed

- Made the portable Ubuntu release gate run from a spawn-importable temporary
  script with the repository root available to parent and child interpreters,
  and guaranteed cleanup of the temporary runner.
- Restored immutable-tag release qualification so build provenance is produced
  only by the successful tag-push workflow after verify-only dispatch passes.

## [0.4.2] - 2026-08-24

### Added

- A dependency-free GitHub Pages landing page and zero-backend synthetic panel
  demo, with reproducible tracked landing and panel screenshots.
- A POSIX release installer that resolves stable GitHub releases, selects exact
  versioned assets, verifies SHA-256 checksums, installs atomically, and can
  optionally install the Agent Sidecar skill bundle.

### Changed

- Updated the synchronized English and Simplified Chinese manuals with the live
  site, panel preview, reviewed installer flow, checksum behavior, and safe
  uninstall guidance.
- Added deterministic site, screenshot, and Pages deployment checks to the
  repository quality and release-governance surface.

### Fixed

- Serialized daemon runtime ownership before stale socket and PID cleanup so
  concurrent starters cannot replace the active owner's artifacts.
- Read Codex SQLite and WAL status through bounded private snapshots without
  mutating the live database or its sidecars.
- Serialized installer install and uninstall operations, including stale-lock
  recovery and signal-safe cleanup.
- Replaced scheduler-sensitive process-stream timing assertions with injected
  monotonic deadlines while preserving timeout and cancellation coverage.
- Qualified release recovery through `refs/tags/<tag>` and required checked-out
  `HEAD` to equal the peeled tag commit.

### Security

- Made release installation fail closed on malformed versions, asset or
  checksum mismatches, unrecognized executable targets, and unowned uninstall
  paths; remote skill files are pinned to the resolved immutable release tag.
- Rejected non-regular transcript sources before adapter parsing so FIFOs and
  other special files cannot block discovery.
- Restricted release attestation and publication to tag-push runs whose source
  ref matches the immutable release tag; manual dispatch is build-and-verify
  only.

## [0.4.1] - 2026-08-24

### Added

- MIT licensing, contribution and security policies, issue and pull-request
  templates, and versioned repository ruleset declarations.
- A canonical local quality runner with Ruff, full tests, coverage policy,
  deterministic packaging smoke tests, CLI checks, and skill checks.
- GitHub Actions CI across the supported Python range, scheduled
  cross-platform regression runs, CodeQL analysis, and Dependabot updates.
- A guarded tag release workflow that verifies version and ancestry, builds
  and cross-platform checks the deterministic zipapp, publishes checksums, and
  attests build provenance.
- Complete English and Simplified Chinese user manuals with installation,
  support, uninstall, security, development, FAQ, release, website, and license
  guidance.

### Changed

- Centralized branch, pull-request, changelog, review, and dual-track
  `main`/`release` procedures in the contribution guide.
- Expanded package metadata and project links while preserving Python 3.9+
  compatibility and an explicit zero-runtime-dependency contract.
- Made bilingual README heading order, command examples, options, and links a
  required governance contract.
- Hardened CI and weekly regression behavior with stable status contexts,
  bounded jobs, explicit platform allowances, and release-compatible checks.

### Fixed

- Made `daemon stop` wait for owned runtime paths and the target process to
  disappear, preventing stale sockets and incomplete shutdown reporting.
- Kept runtime executable validation fail-closed on identity or permission
  changes without rejecting harmless metadata churn in shared ancestors.

### Security

- Added private vulnerability-reporting guidance, supported-version policy,
  trust boundaries, and requirements for sanitizing diagnostics.
- Pinned third-party workflow actions to full commit SHAs, reduced workflow
  permissions, and added bounded concurrency and timeouts.
- Bound release publication to immutable version tags, a CI-green `main`
  ancestry, a fast-forward-only stable release pointer, checksum verification,
  and provenance attestation.

## [0.4.0] - 2026-08-24

### Added

- Concurrent local and remote event watching with host provenance, bounded
  queues, fair merging, and explicit startup readiness.
- Durable, private send auditing with request-ID idempotency and conservative
  process-containment reporting.
- An opt-in read-only HTTP panel and API restricted to numeric IPv4 loopback.
- Deterministic zipapp packaging, `pipx`-ready installation, and explicit
  macOS LaunchAgent service management.
- Private bounded daemon diagnostics with durable error records and rotation.

### Changed

- Hardened session discovery, Cursor SQLite snapshots, remote transport
  cleanup, and failure isolation.

## [0.3.0] - 2026-08-23

### Added

- Local session discovery and normalized event observation across Cursor IDE,
  Cursor CLI, Claude Code, Codex CLI, GitHub Copilot CLI, DSH, and Kimi Code.
- Cursor CLI chat-event decoding and bounded remote `list` and `status`
  aggregation over noninteractive SSH.
- Explicitly gated experimental local session resume for Claude, Codex, and
  Cursor CLI.
- CLI and terminal-dashboard workflows for session listing, status, watching,
  and process inspection.

[Unreleased]: https://github.com/shendeguize/AgentSideCar/compare/v0.11.2...HEAD
[0.11.2]: https://github.com/shendeguize/AgentSideCar/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/shendeguize/AgentSideCar/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/shendeguize/AgentSideCar/releases/tag/v0.3.0
