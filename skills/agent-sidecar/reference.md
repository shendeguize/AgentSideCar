# Agent Sidecar Reference

## Session JSON

`status --json`, `list --json`, and `list --all --json` emit a JSON array of
session objects:

- `agent` (string): adapter name such as `cursor-ide`, `cursor-cli`, `claude`,
  `codex`, `copilot`, `dsh`, or `kimi`.
- `session_id` (string): full session identifier. A unique prefix can be passed
  to `watch` or local `send`.
- `project` (string): project or working-directory path on the originating
  host.
- `transcript` (string): readonly observation source path on the originating
  host.
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

With `list --remote` or `status --remote`, every row also has `host` (string).
`local` identifies local provenance; remote rows use their DSH Center inventory
alias. Without `--remote`, local JSON does not include `host`.

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

## Remote snapshots

`list --remote` and `status --remote` merge local sessions with snapshots from
eligible DSH Center hosts. `--host <host-alias>` is repeatable and
case-insensitive, requires `--remote`, and limits only the remote targets.
Human output adds a `HOST` column.

Inventory comes from `dshc ls --json`, or from the DSH Center config and state
files under `DSHC_HOME` or `~/.dsh_center` when that command is unavailable.
The query uses a bounded zipapp streamed over strict, noninteractive SSH.
Remote hosts need Python 3.9+ but do not need Agent Sidecar or third-party
Python packages installed. Host keys must already be trusted and
noninteractive authentication must already work.

Host failures are isolated and reported on stderr using one of `host_key`,
`auth`, `timeout`, `unreachable`, `python_too_old`, `protocol`, or `remote`.
Successful and local rows remain available during partial failure.

- Exit `0`: at least one requested remote host succeeded, including a partial
  fleet success.
- Exit `2`: invalid inventory, setup, selection, or command usage. Local rows
  are still emitted when available.
- Exit `3`: no requested remote host succeeded. Local rows are still emitted
  when available.

Remote support is limited to `list` and `status`. It does not include `watch`,
`tui`, daemon aggregation, or `send`.

## Local send

`send` is an experimental mutation, not an observation command:

```text
agent-sidecar send <session-prefix> "<message>" --allow-write
agent-sidecar send <session-prefix> "<message>" --allow-write --timeout 120 --json
```

The resolver prefers a sole exact session ID; otherwise the prefix must match
exactly one discovered local session. The target must be a top-level
`waiting` or `idle` session from `claude`, `codex`, or `cursor-cli`. Remote,
`working`, `dead`, child, and sidechain sessions are rejected. `cursor-ide`,
`copilot`, `kimi`, and `dsh` are unsupported.

`send` directly scans local state and starts a separate blocking native
headless-resume process. It does not use the daemon or remote inventory,
interrupt a live process, or fork the original session. The native process may
contact its provider, run tools, and modify agent-owned state. Claude and Codex
receive the native prompt on stdin; Cursor CLI necessarily receives it in the
child argv.

`--allow-write` is mandatory. The message must be nonblank valid Unicode, must
not contain NUL, and must be at most 16 KiB after UTF-8 encoding. `--timeout`
accepts 1 through 900 seconds and defaults to 300. One native resume is
attempted; there is no retry.

The message is present in the sidecar command argv and can be recorded in shell
history or process listings. Cursor CLI also exposes it in its native child
argv. Do not send secrets.

With `--json`, stdout is one object with exactly these fields:

- `agent`: selected native agent.
- `session_id`: full selected session ID.
- `outcome`: `completed`, `failed`, `timed_out`, or `overflow`.
- `delivery`: `delivered` only for native success; otherwise `unknown`.
- `returncode`: native exit status when available, otherwise null.
- `response`: bounded parsed native response text.
- `stderr`: bounded native diagnostic text.
- `error_code`: stable failure code or null.

Without `--json`, success prints the response or a delivery receipt. Runtime
failure prints a delivery-unknown receipt and diagnostics. Output is bounded:
message input is 16 KiB, response capture is 4 MiB, and stderr capture is
64 KiB.

- Exit `0`: `outcome: "completed"` and `delivery: "delivered"`.
- Exit `1`: `failed`, `timed_out`, or `overflow`; delivery is unknown and must
  not be retried automatically.
- Exit `2`: usage, prefix resolution, target eligibility, or other preflight
  rejection before a valid result.
- Exit `130`: interrupted; delivery is unknown.

## Command quick reference

```text
agent-sidecar status --json
agent-sidecar status --remote --json
agent-sidecar status --remote --host <host-alias> --json
agent-sidecar list --json
agent-sidecar list --all --json
agent-sidecar list --remote --json
agent-sidecar list --remote --host <host-alias> --json
agent-sidecar ps --json
agent-sidecar watch <session-prefix> --json
agent-sidecar watch <session-prefix> --from-start --json
agent-sidecar watch --all --json
agent-sidecar send <session-prefix> "<message>" --allow-write
agent-sidecar send <session-prefix> "<message>" --allow-write --json
agent-sidecar tui
agent-sidecar tui --once
agent-sidecar daemon start
agent-sidecar daemon status
agent-sidecar daemon stop
```

Start the daemon only for a user-requested continuous monitor or TUI, or on an
explicit start request. Stop it only on request. Observation commands are
readonly with respect to agent-owned stores. Run remote monitoring only when
the user requests it, and run `send` only for an explicit same-turn request for
that exact message or action.
