# Agent Sidecar

Agent Sidecar is a local-first CLI for observing AI-agent sessions. It
discovers persisted session metadata, infers lifecycle state, follows
normalized events, and presents the result as text, JSON, or a terminal
dashboard. It can also aggregate read-only `list` and `status` snapshots over
SSH and, when explicitly authorized, start an experimental local headless
resume to send a message.

The observation commands do not edit agent transcripts, configuration, or
hooks. `send` is a separate mutating boundary: the resumed native agent may
contact its provider, run tools, and modify agent-owned state. The daemon only
writes its own socket and PID file; the installer only creates integration
symlinks.

Version 0.3 requires Python 3.9+ and has no PyPI dependencies. DSH event
watching additionally requires an external `zstd` executable.

## Supported local sources

The agent names below are also the exact values accepted by `list --agent`:

- `cursor-ide`: Cursor IDE JSONL transcripts and associated terminal metadata.
  Session discovery, inferred status, event watching, and known subagent
  relationships are supported.
- `cursor-cli`: Cursor CLI `store.db`, WAL, and referenced message blobs are
  copied into bounded private snapshots and decoded without opening the live
  database. Discovery, inferred status, history replay, and new normalized
  events are supported.
- `claude`: Claude Code project JSONL transcripts, including known sidechain
  and subagent relationships.
- `codex`: Codex CLI rollout JSONL plus read-only native status SQLite when
  available.
- `copilot`: GitHub Copilot CLI `workspace.yaml` metadata only. It has no event
  source in v0.3 and is reported as `idle`.
- `dsh`: DeepSeek DSH projection-cache metadata for listing and status, plus
  compressed transcript events for watching. Listing and status work without
  `zstd`; watching does not.
- `kimi`: Kimi Code session index, state, and wire JSONL, including discovered
  subagents. `KIMI_CODE_HOME` is honored; otherwise `~/.kimi-code` is used.

All support is best-effort against local, tool-owned persistence formats.
Adapter failures are isolated so one unreadable source does not prevent other
agents from being listed.

## Install or run from the checkout

From the repository root:

```sh
sh scripts/install-skill.sh
```

The installer creates symlinks at:

- `~/.local/bin/agent-sidecar`
- `~/.cursor/skills/agent-sidecar`
- `~/.claude/skills/agent-sidecar`

Ensure `~/.local/bin` is on `PATH`. The links point into this checkout, so keep
the repository in place or rerun the installer after moving it. Reinstalling is
idempotent for links into this repository; the script refuses to overwrite
unrelated files, directories, or symlinks. To remove only links owned by this
checkout:

```sh
sh scripts/install-skill.sh --uninstall
```

Installation is optional. Run directly from the repository instead:

```sh
./agent-sidecar --version
./agent-sidecar status
```

Every example below also works by replacing `agent-sidecar` with
`./agent-sidecar`.

## Commands

Version and session discovery:

```sh
agent-sidecar --version
agent-sidecar list
agent-sidecar list --all
agent-sidecar list --agent cursor-ide --agent claude
agent-sidecar list --all --json
agent-sidecar list --remote
agent-sidecar list --remote --host <host-alias> --json
```

`list` shows sessions updated in the last 48 hours by default. `--all` removes
that age filter. `--agent NAME` is repeatable, exact, and case-insensitive.
`--json` emits a JSON array.

Process and active-status views:

```sh
agent-sidecar ps
agent-sidecar ps --json
agent-sidecar status
agent-sidecar status --json
agent-sidecar status --remote
agent-sidecar status --remote --host <host-alias> --json
```

`ps` reports supported local agent executables; process presence is supporting
evidence and is not reliably attributable to one session. `status` includes
only `working` and `waiting` sessions.

### Remote list and status

`list --remote` and `status --remote` merge the local snapshot with eligible
hosts from DSH Center inventory. `--host <host-alias>` is repeatable,
case-insensitive, and valid only with `--remote`; it limits the remote targets
but never removes local rows.

Remote human output adds a `HOST` column. Remote JSON adds `host` to every row:
`local` marks local provenance, while each remote row carries its
inventory-provided host alias. Local commands without `--remote` retain the
original session schema and do not add `host`.

Inventory is obtained from `dshc ls --json` when available, with the DSH Center
config/state files under `DSHC_HOME` or `~/.dsh_center` as a strict fallback.
Only enabled, nonlocal, non-orphaned hosts in an eligible phase are queried.
The sidecar builds a bounded zipapp from the current checkout and streams it
over noninteractive SSH, so no remote installation or third-party Python
package is required. Remote Python must be 3.9 or newer.

SSH requires an already trusted host key and working noninteractive
authentication. It does not enroll host keys or fall back to an interactive
prompt. The transient zipapp is executed with the remote Python interpreter
and removed after the snapshot.

Remote failures are isolated by host and reported on stderr with stable codes.
Rows from local and successful remote hosts are still printed. A partial fleet
success exits `0`; invalid inventory, setup, or host selection exits `2`; if no
remote host succeeds, the command exits `3`. These commands do not provide
remote watching or message delivery.

Follow one session by a unique ID prefix, or follow all watchable sessions:

```sh
agent-sidecar watch <session-prefix>
agent-sidecar watch <session-prefix> --from-start --json
agent-sidecar watch --all
agent-sidecar watch --all --from-start --json
```

A session prefix and `--all` are mutually exclusive. Without `--from-start`,
only newly observed events are emitted. `--from-start` replays available
history before following. `--json` emits one normalized event object per line.
Direct `--all` fallback skips metadata-only sessions and reports the skipped
count on stderr.

### Experimental local send

`send` is not an observation command. It performs a direct local scan, resolves
an exact session ID or unique prefix, and starts a separate blocking native
headless-resume process:

```sh
agent-sidecar send <session-prefix> "Please review the latest test failure." --allow-write
agent-sidecar send <session-prefix> "Summarize your result." --allow-write --timeout 120 --json
agent-sidecar send <session-prefix> --allow-write -- "-message beginning with a hyphen"
```

Use it only when the user explicitly requests that message or action.
`--allow-write` is mandatory because the native agent can contact its provider,
run tools, and modify session or workspace state. `send` does not use the
daemon or remote inventory, does not interrupt a live process, and does not
fork the original session. It starts another native process that resumes the
persisted session.

Eligible targets are local, top-level `claude`, `codex`, or `cursor-cli`
sessions in `waiting` or `idle`. `working`, `dead`, child, sidechain, and remote
sessions are rejected, as are `cursor-ide`, `copilot`, `kimi`, and `dsh`.
Claude and Codex receive the native prompt on stdin. Cursor CLI necessarily
receives it in the child process argv.

The message must be nonblank UTF-8 without NUL and at most 16 KiB. Timeout is
1–900 seconds and defaults to 300. Execution is bounded and never retried.
Timeout, native failure, or output overflow means delivery is unknown; do not
retry automatically because the agent may already have received or acted on
the message.

On success, human output is the final native response, or a delivery receipt
when no response is available. `--json` emits one result object. Exit `0` means
the native resume completed successfully and delivery is reported as
`delivered`; exit `1` means runtime failure, timeout, or overflow with
`delivery: "unknown"`; exit `2` is a preflight or usage rejection before a
valid resume result; interruption exits `130` and delivery is unknown.

The positional message is present in the `agent-sidecar` command argv and may
be stored in shell history or visible in process listings. For Cursor CLI it is
also present in the native child argv. Do not use this command for secrets.

Open the live terminal dashboard, or render one ANSI-free snapshot:

```sh
agent-sidecar tui
agent-sidecar tui --once
```

Press `q` to leave the interactive dashboard.

Manage the optional per-user daemon:

```sh
agent-sidecar daemon start
agent-sidecar daemon status
agent-sidecar daemon stop
agent-sidecar daemon run
```

`start` launches a detached daemon and is idempotent. `run` runs the same
daemon in the foreground, which is useful under a process supervisor.

## Status semantics

- `working`: fresh persisted evidence indicates an active turn, unresolved tool
  call, running command, or adapter-native in-progress state.
- `waiting`: recent evidence indicates the turn completed and the session
  appears ready for user input or review.
- `idle`: no sufficiently recent activity was observed, or the source reports
  an inactive terminal state.
- `dead`: a required local session source is missing or no longer readable.

These are inferred observations, not agent control-plane guarantees. Some tools
flush metadata or transcripts lazily; Cursor IDE in particular can continue to
appear `working` for several minutes before a completed turn becomes
`waiting`.

## Daemon, fallback, and local protocol

The daemon keeps a session snapshot, scans adaptively, and fans out normalized
events. By default it uses:

- runtime directory: `~/.agent_sidecar` (override with
  `AGENT_SIDECAR_RUNTIME_DIR`);
- Unix socket: `~/.agent_sidecar/daemon.sock`;
- PID file: `~/.agent_sidecar/daemon.pid`.

There is no network listener. The runtime directory is created with user-only
permissions where supported, and the socket and PID file are mode `0600`. The
daemon refuses to replace unsafe non-socket/non-regular paths, and `daemon
stop` signals a process only after the socket PID and PID file agree.

The socket uses bounded newline-delimited JSON requests and responses. Its
operations are `ping`, `status`, and `subscribe`: `status` returns the current
session snapshot and scan errors, while `subscribe` acknowledges the request
and then streams normalized event objects.

The daemon is optional. `list`, `status`, and `tui` prefer its snapshot and
fall back to direct read-only scans when it is unavailable. `watch --all`
prefers a daemon subscription for new events and falls back to direct
multi-session following. A one-session watch is direct, and any
`--from-start` watch uses direct sources so it can replay existing records.

## Agent skill integration

The installed Cursor and Claude skill links expose the same
`skills/agent-sidecar` bundle. The skill instructs agents to query
`agent-sidecar status --json` first for local observation, use remote monitoring
only on request, and start or stop the daemon only when requested. It may run
`send` only for an explicit same-turn request containing the exact message or
action; that request supplies the permission represented by `--allow-write`,
so no second confirmation is required. It never infers send consent from
monitoring and never retries an unknown delivery.

## Development

Run the complete standard-library test suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

## v0.3 scope and deferred work

Version 0.3 provides local observation for the supported sources, Cursor CLI
event watching, remote `list`/`status` snapshots, and experimental local send
for Claude, Codex, and Cursor CLI. Remote watch and remote send are not
implemented. Kimi and DSH injection remain deferred; Cursor IDE and Copilot
send are unsupported. There is no HTTP server, API, or web dashboard, and none
should be inferred from the local Unix-socket protocol.
