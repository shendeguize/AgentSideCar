# Contributing

This file is the single source of truth for development, review, branch
governance, and releases. Other documentation should link here instead of
copying these rules.

## Prerequisites and local gate

- Python 3.9 or newer and Git.
- macOS when changing or validating LaunchAgent-specific behavior.
- An authenticated GitHub CLI (`gh`) only for repository administration.

Agent Sidecar has zero runtime Python dependencies: `[project].dependencies`
must remain an explicit empty array. Ruff and coverage are development-only
tools in the `dev` extra; build tooling is not part of the installed runtime.

Set up a development checkout and run the same quality gate used by CI:

```bash
python3 -m pip install -e '.[dev]'
python3 scripts/check.py
```

The gate runs lint, the full test suite, packaging and zipapp smoke tests, CLI
checks, and skill checks. Run it before opening or updating a pull request.

## Branch and pull-request model

There are two long-lived branches:

- `main` is the green development trunk. All ordinary changes enter it through
  pull requests.
- `release` is the stable release pointer. It only fast-forwards to a
  CI-green commit already on `main` and never contains unique commits. Promotion
  is a direct fast-forward, not a pull request.

Use short-lived branches named
`feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `test/<slug>`, `chore/<slug>`,
`refactor/<slug>`, or `ci/<slug>`. Delete them after merge.

Pull requests target `main`, contain one coherent change, and merge by squash
only. The PR title becomes the commit subject and must match:

```text
^(feat|fix|docs|test|chore|refactor|ci)(\([a-z0-9][a-z0-9._-]*\))?!?: .+
```

Examples: `feat(remote): bound watch startup`, `fix!: reject legacy tokens`, and
`docs: clarify installation`.

## Weekly regression and golden fixture drift

The weekly workflow runs the full Python 3.13 gate on current Ubuntu and macOS
runner images. It catches runner, Python, packaging, CLI, skill, and committed
Cursor-store fixture regressions; it does not read a developer's live private
Cursor store.

To refresh `tests/fixtures/cursor_cli_store_3_16_17_golden.json`, start from a
private local copy of a representative upstream Cursor store and sanitize it
before exporting anything into the repository. Preserve SQLite columns, JSON
key/type shapes, protobuf field numbers/order, and message ordering, but replace
all prompts, responses, tool data, identifiers, paths, timestamps, and numeric
payloads with synthetic fixture values. Recompute each blob SHA-256 plus every
root and metadata reference after replacement, export only the sanitized
`meta`/`blobs` rows as hex, and update the fixture's `provenance` and `expected`
sections.

Fixture changes require review of the decoded hex as well as the JSON: no user,
machine, project, credential, or other private content may remain. The golden
decode, nested mutation, structural/sanitization, and full repository contract
tests must pass:

```bash
python3 -m unittest tests.test_cursor_chat.CursorChatSnapshotTests
python3 scripts/check.py
```

Run the cross-platform regression on demand with:

```bash
gh workflow run weekly.yml --ref main
```

## Changelog and versions

Add every user-observable change to `CHANGELOG.md` under `[Unreleased]` in the
same PR. Describe behavior, compatibility, commands, configuration, protocols,
or security impact rather than implementation details. Pure refactors and test
maintenance with no observable effect may omit an entry.

The version source is `sidecar/__init__.py`; tags are `v<version>`. Follow
Semantic Versioning: MAJOR for incompatible changes, MINOR for compatible
features, and PATCH for compatible fixes. While the project is `0.y.z`, treat
an incompatible public-contract change as a MINOR change and call it out in the
changelog.

Release candidates use `X.Y.Z-rc.N`. They are tagged from a CI-green `main`
commit, the publishing workflow marks them as pre-releases, and they do not
advance the stable `release` branch. The final `X.Y.Z` release advances
`release` normally.

## Review checklist

Record evidence for each applicable item before merge:

| ID | Check |
|---|---|
| RV-1 | Public CLI, JSON, exit-code, configuration, and local protocol contracts remain compatible or the intentional change is documented. |
| RV-2 | Zero runtime dependencies remain intact; new tooling is development-only and justified. |
| RV-3 | Security boundaries remain explicit: loopback-only HTTP, private tokens and sockets, conservative process containment, and opt-in write actions. |
| RV-4 | Remote behavior stays bounded and temporary: noninteractive SSH, Python 3.9+ preflight, cleanup, no required remote install, and isolated host failures. |
| RV-5 | Concurrency, queues, timeouts, output sizes, retries, and cleanup paths remain bounded and deterministic. |
| RV-6 | New paths have tests, fixes have regression coverage, and `python3 scripts/check.py` passes. |
| RV-7 | User-visible changes update `[Unreleased]` plus affected README, skill, reference, or security documentation. |
| RV-8 | The PR has one intent, a valid title, no secrets or private data, and no `.local/` artifacts. |
| RV-9 | Version, changelog, CI context names, rulesets, tags, and release workflow assumptions stay synchronized. |

## Release procedure

1. Open a release PR to `main` titled `chore(release): vX.Y.Z`. It only bumps
   `sidecar/__init__.py`, moves `[Unreleased]` entries into a dated
   `[X.Y.Z]` section, updates comparison links, and adjusts current-version
   documentation where needed.
2. Squash-merge after the local gate and PR checks pass. Wait for the resulting
   `main` push CI run to finish successfully on every matrix entry, including
   `check (macos-latest, Python 3.9)` and Python 3.13.
3. Fetch the remote refs, record the release commit, and fast-forward the
   stable pointer without checking out `release`:

   ```bash
   git fetch origin main release
   release_sha="$(git rev-parse origin/main)"
   git merge-base --is-ancestor origin/release "$release_sha"
   git push origin "$release_sha":release
   ```

   A failed ancestry check or rejected push is a stop condition; investigate
   rather than merge, force-push, or bypass protection.
4. Create the immutable tag on that exact commit and push it:

   ```bash
   git tag "vX.Y.Z" "$release_sha"
   git push origin "vX.Y.Z"
   ```

   The tag-triggered release workflow validates version, changelog, ancestry,
   the checked-out `HEAD` against the peeled tag commit, and artifacts before
   publishing the GitHub Release. Never move, delete, or recreate a published
   `v*` tag.

`workflow_dispatch` is build-and-verify-only recovery diagnostics. It accepts
only a strict existing `vX.Y.Z` or `vX.Y.Z-rc.N` tag, checks out the qualified
`refs/tags/<tag>` ref, and never attests, publishes, uploads to a GitHub
Release, or clobbers release assets. To recover a failed publication, rerun the
original tag-triggered workflow run so provenance remains bound to the tag-push
source; never use a dispatch run to publish.

The existing `v0.4.0` tag predates this dual-track procedure and remains
historical. Do not rewrite it; this procedure governs subsequent releases.

## Governance rule compilation

DevolaFlow is an external repository tool installed in its isolated `uv` tool
environment; it is not a project runtime or development dependency. From the
repository root, regenerate `AGENTS.md` and `.rules/.compile-hashes.json` from
`.rules/compile-config.yaml` with:

```bash
sync-rules
```

Then run the read-only drift check:

```bash
check-rules-drift
```

The drift command exits nonzero when a declared output is missing or its
SHA-256 prefix does not match the compiler-written hash store. Edit canonical
`.rules/*.mdc` sources or the compiler configuration, never generated outputs,
and commit source and generated changes together.

## Repository governance

Rulesets are versioned in `.github/rulesets/`. Required check names are exact
GitHub status contexts: changing the CI job name or matrix requires updating
the matching ruleset JSON. The release ruleset intentionally has no pull
request rule because `release` is promoted by fast-forward; it requires the
achievable `check (macos-latest, Python 3.9)` status from the `main` push.
Bypass lists stay empty.

Repository administrators can create or update all three rulesets
reproducibly with the following script. Run it only after the referenced
status contexts have appeared in GitHub:

```bash
repo="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
for file in \
  .github/rulesets/main.json \
  .github/rulesets/release.json \
  .github/rulesets/tags.json
do
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$file")"
  id="$(gh api "repos/$repo/rulesets" \
    --jq ".[] | select(.name == \"$name\") | .id")"
  if [ -n "$id" ]; then
    gh api -X PUT "repos/$repo/rulesets/$id" --input "$file"
  else
    gh api -X POST "repos/$repo/rulesets" --input "$file"
  fi
done
```

Recommended repository Settings:

- enable secret scanning and push protection;
- enable private vulnerability reporting;
- allow squash merging only and delete merged head branches;
- keep the default Actions token read-only; and
- configure GitHub Pages to deploy from GitHub Actions, not a branch.

`.local/` contains private task, memory, and agent-workspace artifacts and must
never be committed. Governance rules are edited at their canonical `.rules/`
sources and compiler configuration; generated `AGENTS.md` and tool-specific
rule surfaces are compiled outputs. Do not hand-edit compiled surfaces—regenerate
them and commit source and generated changes together.
