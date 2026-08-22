# Agent Sidecar Reference

## Session JSON

`status --json`, `list --json`, and `list --all --json` emit a JSON array of
session objects:

- `agent` (string): adapter name such as `cursor-ide`, `cursor-cli`, `claude`,
  `codex`, `copilot`, `dsh`, or `kimi`.
- `session_id` (string): full session identifier. A unique prefix can be passed
  to `watch`.
- `project` (string): local project or working-directory path.
- `transcript` (string): readonly source path used for observation.
- `updated_at` (number): Unix epoch seconds for the most recent known activity.
- `title` (string): best-effort title derived from session metadata or the
  first user prompt.
- `status` (string): one of `working`, `waiting`, `idle`, or `dead`.
- `extra` (object): adapter-specific readonly metadata. Do not depend on its
  keys without checking the adapter.
- `parent_id` (string or null): parent session identifier when a subagent
  relationship is known.

`status --json` includes only `working` and `waiting` sessions. `list` includes
recent sessions of every status; add `--all` to remove its 48-hour filter.

## Status semantics

- `working`: fresh evidence indicates an active turn, unresolved tool call, or
  adapter-specific in-progress state.
- `waiting`: a fresh turn appears complete and the session is available for
  user input or review.
- `idle`: no sufficiently recent activity was observed.
- `dead`: a required local session source is missing or no longer readable.

Statuses are inferred from persisted local state. They are not commands sent to
the agent. Cursor IDE transcript and terminal updates can make `waiting` appear
several minutes late.

## Event JSON

`watch ... --json` emits newline-delimited event objects:

- `ts` (string): normalized event timestamp.
- `agent` (string): source adapter.
- `session_id` (string): source session identifier.
- `kind` (string): normalized event kind.
- `text` (string): normalized human-readable event text.
- `extra` (object): adapter-specific event metadata.

Without `--from-start`, watch follows newly observed events. `watch --all`
prefers the daemon event stream and falls back to direct multi-session
observation when the daemon is unavailable.

## Process JSON

`ps --json` emits a JSON array with `pid`, `etime`, `exe`, `cwd`, and `cmd`.
Process presence is supporting evidence only; it does not map reliably to one
persisted session.

## Command quick reference

```text
agent-sidecar status --json
agent-sidecar list --json
agent-sidecar list --all --json
agent-sidecar ps --json
agent-sidecar watch <session-prefix> --json
agent-sidecar watch <session-prefix> --from-start --json
agent-sidecar watch --all --json
agent-sidecar tui
agent-sidecar tui --once
agent-sidecar daemon start
agent-sidecar daemon status
agent-sidecar daemon stop
```

Start the daemon only for a user-requested continuous monitor or TUI, or on an
explicit start request. Stop it only on request. All observation is local and
readonly; remote aggregation and agent control are not implemented.
