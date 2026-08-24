# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A native dsh plugin M1 monitoring milestone in the `plugin/` npm
  sub-package: a probe-adopt-else-host daemon supervisor, a Unix-socket
  bridge with snapshot reconciliation, plugin API routes with SSE streaming
  behind a five-layer loopback guard, a cross-agent monitoring board, and a
  settings namespace with its settings card.
- A `send --message-stdin` option that reads the message from standard input
  instead of the positional argument, keeping it out of the `agent-sidecar`
  command line while reusing the same validation, injection pipeline, audit
  identity, receipts, and exit codes. The two message sources are mutually
  exclusive. Interactive terminals are refused with a usage error instead of
  blocking, unreadable standard input reports a dedicated diagnostic, and
  interrupting the read exits `130` cleanly before any delivery.

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

[Unreleased]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.4...HEAD
[0.4.4]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/shendeguize/AgentSideCar/releases/tag/v0.3.0
