# Agent Sidecar

Agent Sidecar is a local observability CLI for AI-agent sessions. It discovers
persisted session metadata, infers lifecycle state, follows normalized events,
and presents the result as text, JSON, or a terminal dashboard.

Observation is read-only: Agent Sidecar does not edit transcripts, agent
configuration, or hooks, and it does not send messages to agents. The daemon
only writes its own socket and PID file; the installer only creates integration
symlinks. Version 0.2 requires Python 3.9+ and has no PyPI dependencies. DSH
event watching additionally requires an external `zstd` executable.

## Supported local sources

The agent names below are also the exact values accepted by `list --agent`:

- `cursor-ide`: Cursor IDE JSONL transcripts and associated terminal metadata.
  Session discovery, inferred status, event watching, and known subagent
  relationships are supported.
- `cursor-cli`: Cursor CLI `store.db` metadata and DB/WAL activity, opened
  read-only. Version 0.2 provides metadata and inferred status only; transcript
  events are unavailable until the deferred blobs task is implemented.
- `claude`: Claude Code project JSONL transcripts, including known sidechain
  and subagent relationships.
- `codex`: Codex CLI rollout JSONL plus read-only native status SQLite when
  available.
- `copilot`: GitHub Copilot CLI `workspace.yaml` metadata only. It has no event
  source in v0.2 and is reported as `idle`.
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
```

`ps` reports supported local agent executables; process presence is supporting
evidence and is not reliably attributable to one session. `status` includes
only `working` and `waiting` sessions.

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
`agent-sidecar status --json` first, use list/process/watch commands only when
needed, and start or stop the daemon only when requested. It preserves the
same observational boundary: no hook installation, configuration changes,
message injection, or transcript writes.

## Development

Run the complete standard-library test suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

## v0.2 scope and deferred work

Version 0.2 is local and read-only. Remote session aggregation, message
injection or other agent control, and an HTTP server/API/dashboard are not
available in v0.2. They are deferred work and must not be assumed from the
local Unix-socket protocol.