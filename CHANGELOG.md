# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Cursor CLI WAL snapshots are normalized only in their private temporary copy
  before read-only decoding, so cleanly exited stores remain readable without
  modifying the source database. Snapshot normalization/open failures now use
  the distinct `CursorChatOpenError` diagnostic.

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

[Unreleased]: https://github.com/shendeguize/AgentSideCar/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/shendeguize/AgentSideCar/releases/tag/v0.3.0
