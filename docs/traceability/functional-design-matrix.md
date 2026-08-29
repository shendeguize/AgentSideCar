# v0.9.0 Functional and Design Traceability Matrix

Version: `v0.9.0-snapshot-1`  
Status: frozen release scope; evidence and test mappings are updated by pull
request only.  
Last reviewed: 2026-08-28

This matrix is the release-facing index for functional behavior, design
invariants, UX requirements, governance, security, release, documentation,
and environment-elimination work. Historical acceptance reports remain
immutable; this file records the current v0.9.0 disposition.

## Status and evidence rules

Each row must have one of these statuses:

- `done`: implementation exists, mapped test or evidence passes, and the
  evidence is reproducible.
- `partial`: some behavior or evidence exists, but the acceptance criterion is
  incomplete.
- `blocked`: the criterion is in scope but cannot currently be verified.
- `manual`: verification is intentionally manual and has dated, versioned
  evidence.
- `N/A`: the criterion is explicitly not applicable and includes a reason.

`release-scope` is `yes` for every row in this v0.9.0 snapshot. A row may be
`N/A`, but no in-scope row may remain `partial` or `blocked` at release.

`test IDs` must be stable IDs declared by the functional selector. Existing
test modules are evidence locations during migration; a file path without a
test ID is not a completed mapping.

## Source register

| Source | Role |
|---|---|
| `README.md`, `README.zh.md` | User-facing behavior, commands, support promises |
| `skills/agent-sidecar/reference.md` | CLI, JSON, socket, remote, and agent integration contracts |
| `CONTRIBUTING.md` | Development, review, CI, version, and release governance |
| `SECURITY.md` | Trust boundaries, privacy, remote and injection posture |
| `.local/tasks/make_dsh_mode/design/dsh_plugin_design.md` | DSH architecture and ADR-1 through ADR-8 |
| `.local/tasks/make_dsh_mode/acceptance/m1_acceptance.md` through `m4_acceptance.md` | Milestone acceptance evidence |
| `.local/tasks/make_dsh_mode/acceptance/s0_smoke.md` | Historical real-environment smoke evidence |
| `.local/tasks/make_dsh_mode/review/ux_review.md` | UX-01 through UX-20 findings |
| `.local/tasks/design_agent_sidecar/build_github_interface/checklist.md` | META, GOV, CI, SEC, REL, DOC, WEB, and AGT checks |
| `.local/tasks/env_elimition/checklist.md` and `design/env_elimination_design.md` | C1-C7 and N1-N6 remote-environment requirements |
| `.local/feedbacks/TRACKER.md` and archived feedback sources | IN-01 through IN-08 regressions |

## Functional units

| ID | Requirement | Test/evidence location | Status |
|---|---|---|---|
| FU-AGENT-001 | Discover supported agent sessions and normalize status | `tests/test_claude.py`, `test_codex.py`, `test_cursor.py`, `test_copilot.py`, `test_dsh.py`, `test_kimi.py` | done |
| FU-CLI-001 | Preserve CLI commands, JSON fields, exit codes, and validation errors | `tests/test_functional_behavior.py` (`FU-CLI-001`), `tests/test_cli.py`, `test_runtime_cmd.py`, `test_tui.py` | done |
| FU-DAEMON-001 | Manage daemon lifecycle, Unix socket ownership, replay, and client cleanup | `tests/test_daemon.py`, `test_client.py` | done |
| FU-EVENT-001 | Publish bounded event-bus records and close subscriptions deterministically | `tests/test_bus.py` | done |
| FU-TAIL-001 | Tail JSONL/Cursor/DSH sources with bounded checkpoints and recovery | `tests/test_functional_behavior.py` (`FU-TAIL-001`), `tests/test_tail.py`, `test_tailer_pool.py`, `test_cursor_chat.py` | done |
| FU-HTTP-001 | Serve read-only loopback HTTP state, events, and sanitized diagnostics | `tests/test_http_server.py`, `test_native_contracts.py` | done |
| FU-REMOTE-001 | Discover, select, execute, aggregate, and isolate remote hosts | `tests/test_remote.py`, `test_remote_watch.py`, `test_payload_compat.py` | done |
| FU-INJECT-001 | Validate target eligibility and execute safe send flows | `tests/test_inject.py`, `test_native_contracts.py` | done |
| FU-AUDIT-001 | Reserve, append, recover, rotate, reset, and rebind private audit state | `tests/test_functional_behavior.py` (`FU-AUDIT-001`), `tests/test_send_audit.py` | done |
| FU-KIMI-001 | Bind Kimi identity, ACP protocol, protected resume, and durable outcome proof | `tests/test_kimi.py`, `test_kimi_acp.py`, `test_kimi_identity.py` | done |
| FU-PROCESS-001 | Inspect processes and enforce bounded containment/cleanup | `tests/test_process.py`, `test_process_runner.py` | done |
| FU-PACK-001 | Build reproducible zipapps and preserve standard-library runtime boundary | `tests/test_release.py`, `test_install_script.py`, `test_governance.py` | done |
| FU-QUALITY-001 | Run lint, tests, coverage, packaging, CLI, skill, and site quality stages | `tests/test_check_script.py`, `test_coverage_gate.py`, `test_site.py` | done |

## Design invariants and ADRs

| ID | Requirement | Evidence location | Status |
|---|---|---|---|
| DS-ADR-001 | Supervisor/daemon ownership uses probe, adopt, or bounded manage states | `dsh_plugin_design.md §4.a`, `plugin/test/supervisor.test.ts` | done |
| DS-ADR-002 | DSH and sidecar data are fused without violating source authority | `dsh_plugin_design.md §4.e`, `plugin/test/fusion.test.ts` | done |
| DS-ADR-003 | Browser/client transport is same-origin JSON/SSE with bounded failure handling | `dsh_plugin_design.md §4.b-§4.c`, `plugin/test/client-integration.test.ts` | done |
| DS-ADR-004 | Injection gateway preserves target guards, confirmation, and audit boundaries | `dsh_plugin_design.md §4.d`, `plugin/test/inject-gateway.test.ts` | done |
| DS-ADR-005 | Index/sidebar integration preserves lineage, grouping, and lifecycle | `dsh_plugin_design.md §4.e`, `plugin/test/board-logic.test.ts` | done |
| DS-ADR-006 | Plugin and Python package boundaries remain independent and public-contract-only | `dsh_plugin_design.md §1`, `CONTRIBUTING.md §dsh plugin development` | done |
| DS-ADR-007 | Skills provider uses documented paths and shares CLI/daemon contracts | `dsh_plugin_design.md §4.f`, `tests/test_skill.py`, `plugin/test/skills-provider.test.ts` | done |
| DS-ADR-008 | Security posture fails closed for local, remote, process, and injection boundaries | `SECURITY.md`, `tests/test_native_contracts.py`, `plugin/test/guard.test.ts` | done |

## DSH milestone acceptance

| ID | Requirement | Evidence location | Status |
|---|---|---|---|
| M1-001 | Install, config layering, and clean pnpm resolution | `m1_acceptance.md §2.1`, `plugin/test/client-build.test.ts` | done |
| M1-002 | Cold-start managed daemon produces board data within bound | `m1_acceptance.md §2.2`, `plugin/test/supervisor.test.ts` | done |
| M1-003 | Pre-existing daemon is adopted and not killed on uninstall | `m1_acceptance.md §2.3`, `plugin/test/supervisor.test.ts` | done |
| M1-004 | Kill/restart backoff reaches failed state and UI degradation | `m1_acceptance.md §2.4`, `plugin/test/supervisor.test.ts` | done |
| M1-005 | Non-loopback, Origin, and non-JSON request guards | `m1_acceptance.md §2.5`, `plugin/test/guard.test.ts`, `plugin/test/routes.test.ts` | done |
| M1-006 | Six agent classes appear consistently with `list --json` | `m1_acceptance.md §2.6`, `plugin/test/board-logic.test.ts` | done |
| M1-007 | HMR reload leaves no orphan process or duplicate route | `m1_acceptance.md §2.7`, `plugin/test/handoff.test.ts` | done |
| M1-008 | Long-running SSE stability and cleanup | `m1_acceptance.md §3`, `s0_smoke.md` | manual |
| M2-001 | Native DSH injection path | `m2_acceptance.md §3-§4`, `plugin/test/dsh-inject.test.ts` | done |
| M2-002 | External-agent CLI injection path | `m2_acceptance.md §3-§4`, `plugin/test/send-cli.test.ts` | done |
| M2-003 | Confirm-token three-way rejection | `m2_acceptance.md §5`, `plugin/test/inject-gateway.test.ts` | done |
| M2-004 | Route and same-turn guard behavior | `m2_acceptance.md §6`, `plugin/test/routes-action.test.ts` | done |
| M2-005 | Audit and failure outcome behavior | `m2_acceptance.md §6`, `tests/test_send_audit.py` | done |
| M2-006 | No-leftover cleanup after injection | `m2_acceptance.md §7`, `plugin/test/dsh-inject.test.ts` | done |
| M3-001 | Timeline cold-read, pagination, and live source consistency | `m3_acceptance.md §①`, `plugin/test/detail-logic.test.ts` | done |
| M3-002 | Gap/degraded behavior is explicit and bounded | `m3_acceptance.md §②`, `plugin/test/client-data.test.ts` | done |
| M3-003 | Lineage, project association, and search | `m3_acceptance.md §③`, `plugin/test/fusion.test.ts`, `project-search-glue.test.ts` | done |
| M3-004 | Real AI analysis is opt-in, bounded, and stoppable | `m3_acceptance.md §④`, `plugin/test/analysis.test.ts` | done |
| M3-005 | Missing `sessionQuery` degrades without corrupting timeline | `m3_acceptance.md §⑤`, `plugin/test/analysis-glue.test.ts` | done |
| M4-001 | Skill filesystem paths and install/uninstall behavior | `m4_acceptance.md §①`, `plugin/test/skills-provider.test.ts` | done |
| M4-002 | Provider path and co-existence behavior | `m4_acceptance.md §②`, `plugin/test/client-build.test.ts` | done |
| M4-003 | better-sidebar dual state | `m4_acceptance.md §③`, `plugin/test/sidebar-tab.test.ts` | done |
| M4-004 | check.py, CI, packaging, and clean-install smoke | `m4_acceptance.md §④-§⑤`, `tests/test_check_script.py`, `tests/test_release.py` | done |
| M4-005 | Registry install and real monitoring/detail smoke | `m4_acceptance.md §final registry smoke`, `s0_smoke.md` | manual |

## UX requirements

| ID | Requirement | Evidence location | Status |
|---|---|---|---|
| UX-01 | Board displays useful session status | `ux_review.md`, `plugin/test/board-logic.test.ts` | manual |
| UX-02 | Board groups sessions meaningfully | `ux_review.md`, `plugin/test/board-logic.test.ts` | manual |
| UX-03 | Sidebar entry and lifecycle are usable | `ux_review.md`, `plugin/test/sidebar-entry.test.ts` | manual |
| UX-04 | Scroll to latest behavior | `ux_review.md` | manual |
| UX-05 | Search interaction | `ux_review.md`, `plugin/test/project-search-glue.test.ts` | manual |
| UX-06 | Empty/loading/error states | `ux_review.md`, `plugin/test/client-data.test.ts` | manual |
| UX-07 | Injection affordance and disabled reasons | `ux_review.md`, `plugin/test/inject-logic.test.ts` | manual |
| UX-08 | Confirmation flow clarity | `ux_review.md`, `plugin/test/inject-gateway.test.ts` | manual |
| UX-09 | Failure feedback and recovery guidance | `ux_review.md`, `plugin/test/routes-action.test.ts` | manual |
| UX-10 | Locale parity | `ux_review.md`, `plugin/test/locales.test.ts` | manual |
| UX-11 | Accessibility labels and state communication | `ux_review.md`, `plugin/test/ui-foundation.test.ts` | manual |
| UX-12 | Detail view scroll-container behavior | `ux_review.md` | manual |
| UX-13 | Timeline rendering | `ux_review.md`, `plugin/test/detail-logic.test.ts` | manual |
| UX-14 | Project/session navigation | `ux_review.md`, `plugin/test/project-view-logic.test.ts` | manual |
| UX-15 | Lineage and source outcome presentation | `ux_review.md`, `plugin/test/fusion.test.ts` | manual |
| UX-16 | Analysis controls and bounded progress state | `ux_review.md`, `plugin/test/analysis-glue.test.ts` | manual |
| UX-17 | Copy-to-clipboard behavior | `ux_review.md` | manual |
| UX-18 | Modal isolation and focus lifecycle | `ux_review.md`, `plugin/test/modal-isolation.test.ts` | manual |
| UX-19 | Group ordering policy | `ux_review.md` | manual |
| UX-20 | HMR and remove/reinsert focus behavior | `ux_review.md`, `plugin/test/handoff.test.ts` | manual |

## Governance, CI, security, release, documentation, web, and agent checks

| ID | Requirement | Evidence location | Status |
|---|---|---|---|
| META-1 | LICENSE exists and is MIT | `tests/test_governance.py` | manual |
| META-2 | Package metadata is complete | `tests/test_governance.py` | manual |
| META-3 | Repository description/topics/homepage are recorded | `checklist.md §META`, hosted settings | manual |
| META-4 | Social preview asset | hosted settings | manual |
| META-5 | `.editorconfig` | repository root | manual |
| META-6 | `.gitattributes` | repository root | manual |
| META-7 | CITATION.cff | checklist decision | N/A: non-academic project |
| GOV-1 | CONTRIBUTING is process source of truth | `CONTRIBUTING.md`, `tests/test_governance.py` | manual |
| GOV-2 | CHANGELOG follows Keep a Changelog | `CHANGELOG.md`, `tests/test_governance.py` | manual |
| GOV-3 | PR template contains required evidence sections | `.github/pull_request_template.md` | manual |
| GOV-4 | PR title convention and CI check | `.github/workflows/ci.yml`, `tests/test_governance.py` | manual |
| GOV-5 | Issue forms and config routing | `.github/ISSUE_TEMPLATE/` | manual |
| GOV-6 | SECURITY.md and private report channel | `SECURITY.md` | manual |
| GOV-7 | CODEOWNERS | hosted repository | manual |
| GOV-8 | CODE_OF_CONDUCT.md | repository root | manual |
| GOV-9 | Labels-as-code synchronization | hosted repository | manual |
| CI-1 | One local/CI staged check entrypoint | `scripts/check.py`, `tests/test_check_script.py` | manual |
| CI-2 | Workflow trigger, permissions, concurrency, matrix, timeout | `.github/workflows/ci.yml` | manual |
| CI-3 | Runtime version matrix | `.github/workflows/ci.yml` | manual |
| CI-4 | Zero runtime Python dependencies | `pyproject.toml`, `tests/test_governance.py` | manual |
| CI-5 | Tiered coverage and suppression policy | `scripts/coverage_gate.py`, `tests/test_coverage_gate.py` | manual |
| CI-6 | Governance guard tests | `tests/test_governance.py` | manual |
| CI-7 | Weekly regression | `.github/workflows/weekly.yml` | manual |
| CI-8 | Workflow static lint | `.github/workflows/ci.yml` | manual |
| CI-9 | Failure artifacts | `.github/workflows/ci.yml` | manual |
| CI-10 | GitHub step summary | `.github/workflows/ci.yml` | manual |
| CI-11 | Language-specific lint in unified gate | `pyproject.toml`, `scripts/check.py` | manual |
| SEC-1 | Workflow least privilege | `.github/workflows/*.yml` | manual |
| SEC-2 | Actions pinned to commit SHA | `.github/workflows/*.yml` | manual |
| SEC-3 | Dependabot configuration | `.github/dependabot.yml` | manual |
| SEC-4 | CodeQL | `.github/workflows/codeql.yml` | manual |
| SEC-5 | Secret scanning and push protection | hosted settings | manual |
| SEC-6 | Private vulnerability reporting | hosted settings, `SECURITY.md` | manual |
| SEC-7 | Build provenance attestation | `.github/workflows/release.yml` | manual |
| SEC-8 | SBOM | checklist decision | N/A: zero-dependency project |
| SEC-9 | OpenSSF Scorecard | hosted workflow | manual |
| SEC-10 | Dependency review | checklist decision | N/A: no third-party runtime dependencies |
| SEC-11 | Harden runner | checklist decision | manual |
| SEC-12 | Local attack surface documentation | `SECURITY.md` | manual |
| REL-1 | Version unique source | `sidecar/__init__.py`, `plugin/package.json` | manual |
| REL-2 | SemVer and RC channel | `CONTRIBUTING.md`, release guard tests | manual |
| REL-3 | Immutable version tags | hosted ruleset, `tests/test_release_guard.py` | manual |
| REL-4 | Protected main/release branches | hosted ruleset | manual |
| REL-5 | Release guard consistency checks | `scripts/release_guard.py`, tests | manual |
| REL-6 | Tag-triggered release workflow | `.github/workflows/release.yml` | manual |
| REL-7 | Release artifacts, checksums, attestation | `.github/workflows/release.yml` | manual |
| REL-8 | Main/release fast-forward model | `CONTRIBUTING.md`, hosted refs | manual |
| REL-9 | Installation channels | `README.md`, install/release tests | manual |
| REL-10 | Support matrix and deprecation policy | `README.md`, `SECURITY.md` | manual |
| REL-11 | Tag signing | hosted repository | manual |
| REL-12 | v0.9.0 delivery convergence | release evidence | manual |
| DOC-1 | README badges and links | `README.md`, `README.zh.md` | manual |
| DOC-2 | README structure baseline | `README.md`, `README.zh.md` | manual |
| DOC-3 | Bilingual README parity | `tests/test_governance.py` | manual |
| DOC-4 | CHANGELOG discipline in PR template | CONTRIBUTING and PR template | manual |
| DOC-5 | Architecture Mermaid diagram | `dsh_plugin_design.md` | manual |
| DOC-6 | Screenshot/demo assets and regeneration | site scripts and acceptance | manual |
| DOC-7 | ADR-lite decision records | design documents | manual |
| DOC-8 | Machine-readable output contracts | reference and contract tests | manual |
| DOC-9 | `llms.txt` site output | site checks | manual |
| WEB-1 | Landing page | site checks and acceptance | manual |
| WEB-2 | Offline synthetic demo | site checks and acceptance | manual |
| WEB-3 | Site build/link/command checks | `tests/test_site.py` | manual |
| WEB-4 | Pages workflow | `.github/workflows/pages.yml` | manual |
| WEB-5 | Favicon/OG/robots/sitemap | site checks | manual |
| WEB-6 | Full Markdown link巡检 | hosted weekly workflow | manual |
| WEB-7 | Lighthouse/accessibility check | hosted workflow | manual |
| AGT-1 | Generated AGENTS governance surface | `AGENTS.md`, governance tests | manual |
| AGT-2 | Canonical rules source and drift protection | `.rules/`, governance tests | manual |
| AGT-3 | Skill/CLI synchronization | `tests/test_skill.py`, governance tests | manual |
| AGT-4 | Upstream format drift fixture | weekly workflow, fixture tests | manual |
| AGT-5 | Demo shim/product UI synchronization | site tests | manual |
| AGT-6 | Work artifact archive boundaries | CONTRIBUTING and governance rules | manual |

## Environment-elimination requirements

| ID | Requirement | Evidence location | Status |
|---|---|---|---|
| ENV-C1 | Remote transport probe/bootstrap research complete | `.local/tasks/env_elimition/research/r1_remote_transport_probe.md` | done |
| ENV-C2 | Local/remote Python minimum versions separated | `research/r2_min_python_feasibility.md` | done |
| ENV-C3 | Contract/governance/test impact mapped | `research/r3_contracts_impact.md` | done |
| ENV-C4 | DSH integration context and fleet profile recorded | `research/r4_dsh_integration_context.md` | done |
| ENV-C5 | Cross-platform packaging options evaluated | `research/r5_packaging_options.md` | done |
| ENV-C6 | Design implements the selected environment-elimination route | `design/env_elimination_design.md`, remote tests | done |
| ENV-C7 | Design review findings closed | `review/design_review.md` | done |
| ENV-N1 | LTS default Python host works with zero configuration | `design/env_elimination_design.md §2.2` | done |
| ENV-N2 | Correct operator remediation is effective | `design/env_elimination_design.md §2.2` | done |
| ENV-N3 | Pod refresh needs no remote persistent setup | `design/env_elimination_design.md §2.2` | done |
| ENV-N4 | No remote system default change is required | `design/env_elimination_design.md §2.2` | done |
| ENV-N5 | Bounded explicit probe and transport discipline | `design/env_elimination_design.md §2.2` | done |
| ENV-N6 | Per-host fail-closed failure posture | `design/env_elimination_design.md §2.2` | done |

## Feedback regressions

| ID | Requirement | Evidence location | Status |
|---|---|---|---|
| IN-01 | Remote observation interpreter compatibility | `.local/feedbacks/archive/feedback_for_v0.6.0.md`, remote tests | done |
| IN-02 | DSH Center inventory fixture and regression | `.local/feedbacks/archive/feedback_for_v0.7.0.md`, remote tests | done |
| IN-03 | Remote rows reach documented `remote_session` rejection | archived IN-03 source, remote/send tests | done |
| IN-04 | `running`/`degraded` hosts remain observable | `from_dsh_center/IN-04-running-phase-observability.md`, remote tests | done |
| IN-05 | Cursor WAL private-copy normalization and clean exit | `from_dsh_center/IN-05-cursor-cli-wal-readonly.md`, Cursor tests | done |
| IN-06 | Injection diagnostics and distinct outcomes | `from_dsh_center/IN-06-inject-failure-diagnosability.md`, inject/plugin tests | done |
| IN-07 | Darwin fork latch reports cleanup uncertainty correctly | `from_dsh_center/IN-07-darwin-fork-latch-cleanup-incomplete.md`, inject tests | done |
| IN-08 | DSH preset gate is target-scoped | `from_dsh_center/IN-08-dsh-preset-gate-host-scoped.md`, plugin tests | done |

## v0.9.0 gap register and disposition

The migration gaps are retained here as an auditable register rather than
silently removed. Local functional, plugin, browser-contract, documentation,
and tracker gaps are closed; hosted security settings and release-run evidence
remain candidate-time checks because they cannot be truthfully verified from an
unpublished working tree.

| Gap | Disposition |
|---|---|
| UX-04, UX-12, UX-17, UX-19 | Closed by the dated browser-contract evidence in `v090-evidence.md` |
| META-4, GOV-7, GOV-8, GOV-9, CI-10 | Repository assets and public workflow evidence recorded; hosted settings remain candidate-time verification |
| SEC-9, SEC-11, REL-11, WEB-6, WEB-7 | Hosted or post-publish checks remain explicit release-candidate gates |
| ENV-N1, ENV-N2 | Closed by the environment/regression selector suite and versioned design evidence |
| IN-01 through IN-08 | Closed by mapped regression tests and the synchronized tracker |
| All `FU-*`, `DS-*`, `M1-*` through `M4-*` rows | Closed by the passing functional selector contract with stable IDs |
| README/README.zh remote-watch eligibility | Closed; both documents describe `ready`, `no_dsh`, `running`, and `degraded` consistently |
| `SECURITY.md` supported versions | Closed; v0.9.x is supported and older lines are unmaintained |
| Historical v0.4.4 checklist versus v0.9.0 scope | Preserved as historical evidence with current disposition recorded here |

## Release completion record

The release PR must attach:

1. functional selector output with no orphan or duplicate IDs;
2. core coverage report at or above 97% for both lines and branches, with
   baseline comparison passing;
3. full Python and plugin test results;
4. browser/manual/hosted evidence references with commit and timestamp;
5. synchronized README, README.zh, SECURITY, CHANGELOG, and TRACKER state;
6. a clean workspace report with no generated artifacts.
