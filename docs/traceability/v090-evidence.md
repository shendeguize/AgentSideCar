# v0.9.0 Evidence Register

This register contains reproducible, content-free evidence pointers for the
functional/design matrix. It must not contain transcripts, session IDs,
machine paths, tokens, screenshots with private data, or raw remote output.

## Browser and UX evidence

| Evidence | Scope | Reproduction | Result |
|---|---|---|---|
| BROWSER-M1 | Install, cold start, adoption, restart, origin guards, six-agent board, HMR | Follow `m1_acceptance.md` sections 1-3 using a synthetic DSH profile | PASS in the recorded M1 report |
| BROWSER-M2 | Prepare/execute injection, confirm-token rejection, audit, cleanup | Follow `m2_acceptance.md` sections 3-7 with synthetic sessions and write disabled by default | PASS in the recorded M2 report |
| BROWSER-M3 | Timeline, pagination, lineage, project/search, degraded analysis | Follow `m3_acceptance.md` sections ①-⑤ with content-free fixtures | PASS in the recorded M3 report |
| BROWSER-M4 | Skills, providers, sidebar, packaging, clean install | Follow `m4_acceptance.md` sections ①-⑤ in an isolated install directory | PASS in the recorded M4 report |
| UX-04 | Scroll-to-latest remains bounded and follows the active timeline | `pnpm test -- --run plugin/test/client-integration.test.ts` | Covered by scroll/focus contract tests |
| UX-12 | Detail view uses the owned scroll container | `pnpm test -- --run plugin/test/client-integration.test.ts` | Covered by scroll/focus contract tests |
| UX-17 | Copy feedback is cancellable and does not leak timers | `pnpm test -- --run plugin/test/client-integration.test.ts` | Covered by clipboard lifecycle contract tests |
| UX-19 | Group ordering is deterministic | `pnpm test -- --run plugin/test/commands.test.ts plugin/test/project-view-logic.test.ts` | Covered by ordering contract tests |

The browser rows are evidence of UI behavior, not authorization to publish
local paths or real session content. Re-run them only with synthetic data.

## Hosted evidence

| Evidence | Scope | Source of truth | Result |
|---|---|---|---|
| HOSTED-PUBLIC-2026-08-29 | Public repository URL, description, homepage, and topics | `https://github.com/shendeguize/AgentSideCar` | PASS; verified 2026-08-29 with `gh api` |
| HOSTED-WORKFLOWS-2026-08-29 | CI, CodeQL, Pages, Plugin, Release, and Weekly workflows | `https://github.com/shendeguize/AgentSideCar/actions` | PASS; all required workflows are active |
| HOSTED-REPO | Description, topics, homepage, issue forms, labels | Repository settings and `.github/ISSUE_TEMPLATE/` | Verify on the v0.9.0 release candidate |
| HOSTED-SECURITY | Secret scanning, push protection, private vulnerability reporting | Repository security settings and `SECURITY.md` | Verify on the v0.9.0 release candidate |
| HOSTED-BRANCHES | Protected `main`, fast-forward-only `release`, immutable `v*` tags | Repository rulesets | Verify before promotion |
| HOSTED-RELEASE | Release asset, checksum, provenance, and source commit | Tag-triggered release workflow | Verify after tag build |
| HOSTED-PAGES | Landing page, synthetic demo, metadata, and `llms.txt` | Pages workflow and site checks | Verify after Pages build |

Hosted checks must record the public repository URL, workflow run URL or
ruleset name, commit/tag, date, and pass/fail result in the release PR. Never
copy credentials, private settings, raw logs, or local screenshots into this
repository.

## Verification command set

```bash
python3 scripts/functional_matrix.py check
python3 scripts/functional_matrix.py run
python3 scripts/check.py --fast
cd plugin && pnpm typecheck && pnpm test && pnpm build
```

The release command set is intentionally separate from the evidence register:
publishing or changing hosted state requires the release procedure in
`CONTRIBUTING.md` and must not be inferred from a local test result.
