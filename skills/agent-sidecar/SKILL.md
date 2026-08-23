---
name: agent-sidecar
description: Monitors readonly local AI agent sessions and reports their state and progress, with remote monitoring only on request and explicit local send support. Use when the user asks for agent status, session progress, to monitor agents, which agent is waiting or working, or explicitly asks to send a message or feedback to an agent.
---

# Agent Sidecar

Use `agent-sidecar` to inspect agent sessions without changing their
transcripts, configuration, or hooks. Observation is the default. `send` is an
experimental, explicitly authorized mutation that starts a separate native
headless-resume process.

## Observation workflow

1. Check `command -v agent-sidecar`.
   - If it is missing, do not install anything unless the user explicitly asks.
     Point them to the documented alternatives: `pipx install .` from a
     checkout, `pipx install 'git+https://github.com/shendeguize/AgentSideCar.git'`,
     the deterministic zipapp, or `scripts/install-skill.sh` for checkout
     symlinks. Do not claim a PyPI-hosted package.
2. Run `agent-sidecar status --json` first. Summarize the active sessions by
   agent, status, title, project, and age from `updated_at`.
3. Use another observation command only when it matches the request:
   - `agent-sidecar list --json` lists sessions updated in the last 48 hours.
   - `agent-sidecar list --all --json` includes older sessions.
   - `agent-sidecar ps --json` checks supported local agent processes.
   - `agent-sidecar watch <session-prefix> --json` follows one session.
   - `agent-sidecar watch --all --json` follows normalized events from all
     sessions. If an established daemon stream drops, it warns before switching
     to direct tailing because transition events may be missed.
   - `agent-sidecar watch --all --remote --json` concurrently follows local
     sessions and eligible remote hosts, but use it only for an explicit remote
     or cross-host watch request.
   - `agent-sidecar tui` opens the interactive terminal dashboard; `q` exits.
4. Start the daemon with `agent-sidecar daemon start` only when the user asks
   for continuous monitoring, the TUI, or explicitly asks to start it. The
   command is idempotent. Status and list queries work without it by falling
   back to direct readonly scans. The daemon writes its socket and PID plus
   bounded, ephemeral private temporary snapshots and rotating private
   `daemon.jsonl` diagnostics. Only explicit HTTP startup additionally retains
   private `http.token` and owns transient `http.port` while running. These
   daemon observation writes make no persistent transcript or agent
   configuration changes. This normal start remains Unix-socket only.
5. Stop the daemon with `agent-sidecar daemon stop` only when the user asks.

## Packaging and macOS service boundaries

`agent-sidecar package build --output dist/agent-sidecar.pyz` creates a
deterministic, executable zipapp and prints its path, SHA-256 digest, and size.
It can be run directly or with `python3 -I`. A pipx console script, executable
zipapp, and checkout shim all resolve `daemon start` to a stable absolute
runtime command, so background startup is independent of the caller's current
working directory.

On macOS, `agent-sidecar service status` is a read-only combined LaunchAgent
and daemon health query and may be used when it helps answer a status or
diagnostic request. Service label `com.agent-sidecar.daemon` is installed in
the current user's `gui/<uid>` domain with `RunAtLoad` and `KeepAlive`.

Never run `agent-sidecar service install`, `service install --force`, or
`service uninstall` unless the user explicitly requests that service mutation
in the current turn. Continuous monitoring, daemon start, HTTP, status, or
packaging requests do not imply permission to install a persistent service.
Service installation is never automatic. Service control is unsupported on
non-Darwin systems.

When explicitly requested, install with `agent-sidecar service install`; add
`--http` and optional `--http-port <port>` only when the user also explicitly
requests HTTP. A changed validated definition requires `--force`, which
unloads and replaces the job and can interrupt monitoring. Warn that rollback
is attempted but can be incomplete before using `--force`.

For updates, uninstall with the old command before upgrading or moving its
pipx environment, zipapp, or checkout, then reinstall with the desired HTTP
flags. This guarantees a process reload and avoids leaving a plist that points
to a moved runtime. Service uninstall retains the private runtime directory,
including daemon diagnostic and HTTP-token data. Any retained send-audit state
is CLI-owned, not a daemon observation write. Do not delete the runtime as part
of service removal.

## Explicit HTTP panel workflow

Enable HTTP only when the user explicitly asks for the HTTP API or browser
panel. Use `agent-sidecar daemon start --http`; add `--http-port <port>` only
when the user selects a value from `0` through `65535`. Omitted or zero means
an ephemeral port. `daemon run` accepts the same flags for a foreground daemon.
Do not add HTTP merely for ordinary status, list, watch, TUI, or continuous
monitoring requests; those retain the Unix-daemon default.

After background readiness, report only the `http://127.0.0.1:<port>` URL and
the `http.token` file path printed by `daemon start` or `daemon status`. Never
read, copy, echo, summarize, log, or send the token into chat, and never place
it in a command, URL, or other tool argument. Tell the user to open the URL and
paste the token from the private file into the panel themselves. The form
clears the field and keeps the token only in page memory; the display retains
at most 200 events.

The listener is read-only and numeric-IPv4-loopback-only at `127.0.0.1`, never
remote or LAN. Its authenticated surface is status plus an NDJSON event stream;
it has no send, reset, control, or CORS surface. Strict Host/Origin validation,
bounded clients and streams, private mode-`0600` token/port files, and
one-process/port ownership apply. Unsafe HTTP metadata fails startup. Stop the
daemon with `agent-sidecar daemon stop` when the user asks; it closes streams
and releases the owned listener and port record.

For remote monitoring, run `list --remote --json`, `status --remote --json`, or
`watch --all --remote --json` only when the user explicitly asks for remote or
cross-host results. Add repeatable `--host <host-alias>` filters only when the
user identifies those hosts. Remote rows and events include a `host` provenance
field; local values are marked `local`. Human remote-watch output prepends a
host column.

Remote `list` keeps the 48-hour default; add `--all` only when full history is
requested. Remote watch always requires `--all`, follows local and remote
sources concurrently, and applies `--from-start` to both sides when requested.
It cannot watch a remote session prefix. It uses eligible DSH Center hosts with
Python 3.9+, strict noninteractive SSH, an ephemeral zipapp, and bounded queues
that backpressure producers.

Do not use remote mode by default. Never hide a remote failure or
`events may be missed`/transition-gap warning. Remote watch does not reconnect
or retry automatically; do not add an agent-level retry. Report the failure and
continue observing surviving streams. On `Ctrl-C`, let the command perform its
bounded SSH, worker, and temporary-file cleanup and report exit `130`.

For remote snapshots, an empty eligible fleet exits `0` with a local-only
notice. For remote watch it continues locally and exits `0` when a usable local
source exists, but exits `1` when neither local nor remote sources are usable.
Remote-watch exit `3` means every host in a nonempty fleet failed and there was
no usable local source. Exit `2` is usage, inventory, setup, or selection
failure. Per-host `resource_limit` diagnostics indicate bounded-data failures.

## Explicit send workflow

Run `send` only when the user explicitly requests the exact message or action
in the same turn. Never infer consent from a request to observe, watch, report,
or wait. If the request is ambiguous, ask for the exact target and message
instead of sending.

An explicit same-turn request to send the exact message is the permission
required to use `--allow-write`; do not ask for a second confirmation. Never
construct or add `--allow-write` without that explicit send request, and pass
the requested message without embellishment.

Before sending, use readonly discovery if needed to resolve the target. Then
choose a fresh stable, unique, opaque request ID that can be retained by the
calling workflow, and run exactly one command:

```sh
agent-sidecar send <session-prefix> "<exact-message>" --allow-write --request-id "<stable-unique-id>" --json
```

`--request-id` is optional and Sidecar generates a cryptographically random ID
when it is omitted, but callers should supply a stable unique ID for safe
deduplication if an invocation might be repeated. Preserve the returned
`request_id` and `replayed` fields in the report. The same retained ID with the
same target, project, and exact message returns a replayed receipt without
spawning. A changed target, project, or message returns `request_conflict`.
Never recycle an ID for unrelated work.

The only eligible targets are local, top-level `claude`, `codex`, and
`cursor-cli` sessions in `waiting` or `idle`. Never send to a remote,
`working`, `dead`, child, or sidechain session. Never send to `cursor-ide`,
`copilot`, `kimi`, `dsh`, or another unsupported agent. `send` performs its own
direct local scan; it does not use the daemon or remote aggregation.

Compatibility was rechecked on 2026-08-23: Kimi Code 0.38 remains unsupported
because resumed print mode auto-approves permissions and exposes the prompt in
argv. DSH 0.1.1-rc.2 remains unsupported because it has neither session resume
nor stdin prompt transport. Do not substitute machine-specific executable
paths for this compatibility rule.

`waiting` and `idle` are inferred and can lag; they do not prove that no native
process is using the session. Do not send to a session that may still be open or
active in a separately launched interactive agent, even if Sidecar reports it
as `waiting`.

Before spawning, `send` acquires a nonblocking, hashed per-session POSIX lock in
the private runtime directory, performs a fresh direct scan and target
revalidation under the lock, and holds the lock through the native process. It
rejects concurrent Sidecar sends and targets that changed or disappeared. The
lock does not coordinate with separately launched native interactive agents.
The resumed agent may contact its provider, run tools, and modify agent-owned
state or workspace files.

Mutating send requires deterministic descendant containment. On Darwin,
Sidecar starts a gated supervisor and installs a `kqueue` monitor for
root-process fork activity before releasing the native target. A no-fork
completion can be delivered. Any observed fork is conservatively returned as
`error_code: "cleanup_incomplete"` with `delivery: "unknown"`, even when tool
work was synchronous, the native process exited zero, or response text was
captured. State plainly that delivery is unknown; do not describe the send,
response, or tool work as successfully delivered, and never retry it
automatically.

On platforms without the required containment primitives, send returns
`containment_unsupported` with delivery unknown before the native target
executes. There are no whole-process scans or unverified descendant kills;
Sidecar limits signaling to the owned root/process group. Do not add process
scans, PID-based cleanup, or an agent-level retry. This containment does not
remove the residual race with a separately launched native interactive agent.

Every new send writes and synchronizes a pending reservation before spawn and a
terminal receipt afterward in the private send-audit store. The store uses
mode-`0600` `audit.jsonl`, one bounded `audit.jsonl.1` rotation, and a private
`audit.key` under the mode-`0700` runtime namespace. Records contain opaque
request/target HMACs and receipt metadata, but no message, response/content,
unkeyed content or identity hash, filesystem path, stdout, or stderr.
Idempotency lasts only while the request remains in the current or rotated log.
The namespace is pinned through a private anchor under the effective account's
configured home rather than trusting `HOME`; unsafe path, ownership, mode,
inode, or key changes fail closed. Send requires the necessary POSIX
descriptor-relative, no-follow, and locking primitives.

An audit failure before spawn fails closed and no native process runs. An audit
failure after spawn reports `audit_error` with delivery unknown. A replayed
`request_pending` means a prior process may have crashed after reservation; it
is delivery unknown and must not spawn or retry automatically.

Never retry `failed`, `timed_out`, `overflow`, `request_pending`,
`audit_error`, `cleanup_incomplete`, an interrupted command, or any result
with `delivery: "unknown"`. The agent may already have received or acted on the
message. Report the unknown state plainly and ask the user what to do next.

The message is a positional argument in the sidecar command, so it can appear
in shell history and process listings. Claude and Codex pass the native prompt
through stdin, but Cursor CLI necessarily includes it in the child argv too.
Warn against sending secrets through this command.

Never invoke `agent-sidecar audit reset` automatically, including in response
to `audit_corrupt`, `audit_error`, or `unsafe_lock`. It is the only recovery
operation and must run only when the user explicitly requests that destructive
recovery in the current turn:

```sh
agent-sidecar audit reset --allow-write --confirm CLEAR-SEND-AUDIT
```

Reset refuses an active send and irreversibly deletes the retained audit and
request-ID idempotency history. Report that consequence before acting.

Outside an explicit send request, never edit agent configuration, install
hooks, inject messages, or modify transcript stores.

## Interpretation limits

- Cursor CLI reads normally use bounded private temporary DB/WAL snapshots. A
  metadata fallback may open the live main database with
  `mode=ro&immutable=1`, without reading its WAL or taking SQLite locks.
- Daemon-backed observation may report bounded, sanitized tailer diagnostics
  on stderr; include them when they are relevant to the user's request.
- The mode-`0700` runtime contains bounded mode-`0600` `daemon.jsonl` plus two
  rotating backups. Records contain allowlisted operational fields and stable
  privacy-safe codes, not transcript/message content, responses, paths,
  tokens, cookies, environment values, stdout, or stderr. Tail errors are
  durable. A logging failure disables logging but not the daemon and appears
  as a path-free `daemon_log` diagnostic.
- `audit.jsonl`, `audit.key`, send locks, and the audit namespace anchor are
  created or managed only by explicit CLI send/audit operations, not daemon
  observation.
- Cursor IDE can report `waiting` several minutes late because transcript and
  terminal metadata are flushed asynchronously.
- Remote watch supports only `watch --all --remote`; remote session-prefix
  watch and remote send are unavailable.
- Treat `working` and `waiting` as inferred observations, not control-plane
  guarantees. Use `ps --json` as supporting process evidence when needed.

For JSON schemas, status semantics, and the command quick reference, read
[reference.md](reference.md).
