# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/shendeguize/AgentSideCar/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/shendeguize/AgentSideCar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/shendeguize/AgentSideCar/releases/tag/v0.3.0
