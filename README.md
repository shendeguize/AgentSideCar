# Agent Sidecar

[English](README.md) | [简体中文](README.zh.md)

[![CI](https://github.com/shendeguize/AgentSideCar/actions/workflows/ci.yml/badge.svg)](https://github.com/shendeguize/AgentSideCar/actions/workflows/ci.yml)
[![Python >=3.9](https://img.shields.io/badge/Python-%3E%3D3.9-3776AB?logo=python&logoColor=white)](https://github.com/shendeguize/AgentSideCar/blob/main/pyproject.toml)
[![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](https://github.com/shendeguize/AgentSideCar/blob/main/pyproject.toml)
[![Release](https://img.shields.io/github/v/release/shendeguize/AgentSideCar?display_name=tag)](https://github.com/shendeguize/AgentSideCar/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/shendeguize/AgentSideCar/blob/main/LICENSE)

[Latest release](https://github.com/shendeguize/AgentSideCar/releases/latest) ·
[Website/demo](https://shendeguize.github.io/AgentSideCar/) (the planned GitHub
Pages destination; deployment follows this documentation change)

Agent Sidecar is a local-first CLI for observing AI-agent sessions. It
discovers persisted session metadata, infers lifecycle state, follows
normalized events, and presents the result as text, JSON, or a terminal
dashboard. It can also aggregate read-only `list` and `status` snapshots over
SSH, concurrently follow local and remote events, and, when explicitly
authorized, start an experimental local headless resume to send a message. An
optional HTTP panel and API expose read-only daemon data on local IPv4 loopback.

The observation commands do not edit agent transcripts, configuration, or
hooks. `send` is a separate mutating boundary: the resumed native agent may
contact its provider, run tools, and modify agent-owned state. The daemon writes
its own socket and PID file, bounded ephemeral private temporary snapshots, and
rotating private diagnostics. Only when HTTP is explicitly enabled does it also
retain a private `http.token` and own a transient `http.port` record while
running. Send-audit files are written by the mutating CLI `send` path, not by
daemon observation. Those daemon and audit bookkeeping files do not themselves
edit transcripts or agent configuration; a resumed native agent can do so as
described above. The installer only creates integration symlinks.

Version 0.4.1 requires Python 3.9+ and has no runtime Python dependencies. DSH
event watching additionally requires an external `zstd` executable.

## Version 0.4.1

Version 0.4.1 completes concurrent local/remote `watch --all --remote`, adds
durable private send auditing and request-ID idempotency, and provides the
opt-in numeric-loopback HTTP panel. It also adds a deterministic executable
zipapp, package metadata suitable for `pipx`, an explicit macOS user
LaunchAgent, and bounded private daemon diagnostics with log rotation.

<a id="support-matrix"></a>

## Support matrix

| Environment | Support boundary |
| --- | --- |
| macOS | Primary platform and full quality-gate target. Local observation, remote monitoring, the daemon and HTTP panel, deterministic zipapps, experimental local `send`, and the user LaunchAgent are supported. |
| Linux | Portable observation, remote monitoring, daemon/HTTP, TUI, and packaging paths are exercised in CI. The macOS LaunchAgent is unavailable, and experimental `send` fails closed before execution because its required Darwin `kqueue` descendant containment is unavailable. Linux support is best-effort where agent-owned persistence formats or desktop integrations differ. |
| Windows | Unsupported. The current runtime and security contracts require POSIX permissions, file locking, process groups, Unix sockets, and related primitives. |
| Python | Python 3.9 or newer is required locally and on SSH targets. CI exercises Python 3.9 and 3.13; Agent Sidecar has zero runtime Python dependencies. |

Support for an operating system does not imply support for every observed
agent on that system. The source-specific boundaries below and the mutating
`send` restrictions remain authoritative.

## Supported local sources

The agent names below are also the exact values accepted by `list --agent`:

- `cursor-ide`: Cursor IDE JSONL transcripts and associated terminal metadata.
  Session discovery, inferred status, event watching, and known subagent
  relationships are supported.
- `cursor-cli`: Cursor CLI `store.db` and its WAL are normally copied into
  bounded private temporary snapshots before decoding. A metadata fallback may
  instead open the live main database with SQLite `mode=ro&immutable=1`; that
  fallback does not read the WAL or take SQLite locks. Discovery, inferred
  status, history replay, and new normalized events are supported.
- `claude`: Claude Code project JSONL transcripts, including known sidechain
  and subagent relationships.
- `codex`: Codex CLI rollout JSONL plus read-only native status SQLite when
  available.
- `copilot`: GitHub Copilot CLI `workspace.yaml` metadata only. It has no event
  source in v0.4.1 and is reported as `idle`.
- `dsh`: DeepSeek DSH projection-cache metadata for listing and status, plus
  compressed transcript events for watching. Listing and status work without
  `zstd`; watching does not.
- `kimi`: Kimi Code session index, state, and wire JSONL, including discovered
  subagents. `KIMI_CODE_HOME` is honored; otherwise `~/.kimi-code` is used.

All support is best-effort against local, tool-owned persistence formats.
Adapter failures are isolated so one unreadable source does not prevent other
agents from being listed.

## Installation

Choose one CLI installation method. Agent Sidecar is not documented as
published on PyPI, so do not assume that `pipx install agent-sidecar` resolves
to this project.

### Install with pipx

Install the console command from an existing Git checkout:

```sh
pipx install .
```

Or install directly from Git:

```sh
pipx install 'git+https://github.com/shendeguize/AgentSideCar.git'
```

For a released, immutable revision, append its tag, for example `@v0.4.1`, to
the Git URL after that tag is available. Both forms create an isolated
environment and install `agent-sidecar`; the package has no runtime Python
dependencies.

### Install a GitHub Release zipapp

GitHub Releases publish the executable zipapp and its checksum file. For
version 0.4.1:

```sh
version=0.4.1
curl -fLO "https://github.com/shendeguize/AgentSideCar/releases/download/v${version}/agent-sidecar-${version}.pyz"
curl -fLO "https://github.com/shendeguize/AgentSideCar/releases/download/v${version}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS
chmod +x "agent-sidecar-${version}.pyz"
./agent-sidecar-${version}.pyz --version
```

Use the matching checksum file from the same release. To expose the verified
artifact as `agent-sidecar` without a package installation:

```sh
mkdir -p "$HOME/.local/bin"
install -m 0755 "agent-sidecar-${version}.pyz" "$HOME/.local/bin/agent-sidecar"
agent-sidecar --version
```

Ensure `~/.local/bin` is on `PATH`. The release workflow verifies the zipapp on
macOS before publication and attaches build provenance. Release artifacts do
not represent a PyPI publication.

### Build the deterministic zipapp

Build through the active Agent Sidecar command:

```sh
agent-sidecar package build --output dist/agent-sidecar.pyz
```

From a checkout, `./agent-sidecar package build --output
dist/agent-sidecar.pyz` is equivalent.

The command atomically creates a mode-`0755` executable and prints its path,
SHA-256 digest, and size. It packages the active installed `sidecar` package:
checkout sources when run through that checkout, installed site-packages when
run through pipx or a wheel environment, and the embedded package when run from
an existing zipapp. It does not implicitly package the shell's current working
directory. Repeated builds from identical active package content are
deterministic. Run it directly or through isolated Python:

```sh
./dist/agent-sidecar.pyz --version
python3 -I dist/agent-sidecar.pyz --version
```

If an artifact lost its executable bit during transfer, restore it with
`chmod +x agent-sidecar.pyz` before using it to start a background daemon or
install a service.

### Use a repository checkout

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
unrelated files, directories, or symlinks. See [Uninstall](#uninstall) to
remove only links owned by this checkout.

This checkout installer is retained for users who want the CLI and both agent
skill links to follow a working tree. It is an alternative to a `pipx` CLI
install because both normally use `~/.local/bin/agent-sidecar`.

Installation is optional. Run directly from the repository instead:

```sh
./agent-sidecar --version
./agent-sidecar status
```

Every example below also works by replacing `agent-sidecar` with
`./agent-sidecar`.

`daemon start` resolves the active pipx console script, executable `.pyz`, or
checkout shim to a stable absolute command before detaching. The daemon
therefore starts independently of the shell's current working directory. Keep
a checkout or zipapp at the resolved location while it is in use; follow the
service update procedure below before moving or removing it.

<a id="uninstall"></a>

## Uninstall

Use only the steps that match how Agent Sidecar was installed. Stop persistent
processes before removing the executable they use:

```sh
agent-sidecar service uninstall
agent-sidecar daemon stop
```

`service uninstall` applies only to the macOS LaunchAgent. It retains the
private runtime directory, diagnostics, HTTP token, and any send-audit files.
For a `pipx` installation:

```sh
pipx uninstall agent-sidecar
```

For links created by the checkout installer, run this from that checkout:

```sh
sh scripts/install-skill.sh --uninstall
```

For a release zipapp copied manually to the exact path shown above, remove that
file only after verifying it is the Agent Sidecar artifact you installed:

```sh
rm "$HOME/.local/bin/agent-sidecar"
```

Deleting `~/.agent_sidecar` is not part of normal uninstall. It can destroy
diagnostics, the HTTP token, send-audit records, and request-ID idempotency
history. Do not use `audit reset` as an uninstall command.

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
Remote `list` uses the same 48-hour default as local `list`; `--all` requests
the full available history from both.
The sidecar builds a bounded zipapp from its active installed `sidecar` package
and streams it over noninteractive SSH. That source is the checkout when run
there, installed site-packages under pipx or a wheel, or the embedded package
under a zipapp. No remote installation or third-party Python package is
required. Remote Python must be 3.9 or newer.

SSH requires an already trusted host key and working noninteractive
authentication. It does not enroll host keys or fall back to an interactive
prompt. The transient zipapp is executed with the remote Python interpreter
and removed after the snapshot.

Remote failures are isolated by host and reported on stderr with stable codes,
including `resource_limit` for bounded-data violations. Rows from local and
successful remote hosts are still printed. A partial fleet success exits `0`.
An empty eligible fleet also exits `0` with a notice that only local sessions
are shown. Invalid inventory, setup, or host selection exits `2`; exit `3`
applies only when a nonempty requested fleet has zero successful hosts. These
snapshot commands do not provide remote message delivery.

Follow one session by a unique ID prefix, or follow all watchable sessions:

```sh
agent-sidecar watch <session-prefix>
agent-sidecar watch <session-prefix> --from-start --json
agent-sidecar watch --all
agent-sidecar watch --all --from-start --json
agent-sidecar watch --all --remote
agent-sidecar watch --all --remote --host <host-alias>
agent-sidecar watch --all --remote --from-start --json
```

A session prefix and `--all` are mutually exclusive. Without `--from-start`,
only newly observed events are emitted. `--from-start` replays available
history before following. `--json` emits one normalized event object per line.
Direct `--all` fallback skips metadata-only sessions and reports the skipped
count on stderr.

### Remote watch

`watch --all --remote` follows local sessions and all eligible remote hosts
concurrently, then fairly merges their events into one stream. `--host
<host-alias>` is repeatable, case-insensitive, and limits only the remote side;
local sessions remain included. `--from-start` applies to both local and remote
sources. Remote mode requires `--all`: watching a remote session by ID prefix
is unsupported.

Every event has host provenance in remote mode. JSON output adds `host` to each
event (`local` for local events and the inventory alias for remote events);
human output prepends a fixed-width host column. Local watch commands without
`--remote` keep the original event schema and presentation.

Remote watch uses the same DSH Center inventory and eligibility rules as remote
snapshots: hosts must be enabled, nonlocal, non-orphaned, and in the `ready` or
`no_dsh` phase. Each host is probed for Python 3.9+ before transfer. The sidecar
then streams a bounded zipapp built deterministically from the active installed
`sidecar` package over strict, noninteractive SSH. The zipapp is written to a
private temporary file on the host, preflighted, run in isolated Python mode,
and removed during cleanup; no remote Agent Sidecar installation or
third-party Python package is required.

Local, remote, and per-host queues are bounded. Producers apply backpressure
rather than dropping queued events, and fair draining prevents a busy host
from starving its peers. After the remote version preflight, readiness means
the watch child produced a valid event or remained alive for a one-second
grace period. Because the child has no internal ready signal, a silent
initialization failure after that grace can still be reported after readiness.

There is no automatic reconnect or retry. A terminal host failure is printed
on stderr with a stable code and an explicit `events may be missed` warning;
local and successful peer streams continue. If the local daemon subscription
drops, the existing local fallback warning likewise calls out the possible
transition gap. Do not hide either warning. `Ctrl-C` cancels both sides, closes
SSH process groups, joins watch workers within bounded cleanup deadlines,
removes remote temporary files, and exits `130`.

Remote-watch exit codes are:

- `0`: normal completion or partial remote failure when a usable local source
  or successful remote stream remains.
- `1`: no usable local source and an empty eligible remote fleet, or another
  local/output runtime failure.
- `2`: invalid usage, inventory, setup, or host selection.
- `3`: a nonempty remote fleet had all hosts fail and no usable local source
  remained.
- `130`: interrupted by the user.

An empty eligible fleet prints `remote: no eligible hosts; showing local
sessions only` and continues local watching when possible; without a usable
local source it exits `1`. Remote message delivery remains unsupported.

### Experimental local send

`send` is not an observation command. It performs an initial direct local scan
and resolves an exact session ID or unique prefix:

```sh
agent-sidecar send <session-prefix> "Please review the latest test failure." --allow-write
agent-sidecar send <session-prefix> "Summarize your result." --allow-write --timeout 120 --json
agent-sidecar send <session-prefix> "Retry-safe request." --allow-write --request-id <stable-unique-id> --json
agent-sidecar send <session-prefix> --allow-write -- "-message beginning with a hyphen"
```

Use it only when the user explicitly requests that message or action.
`--allow-write` is mandatory because the native agent can contact its provider,
run tools, and modify session or workspace state.

`--request-id` is optional. When omitted, Sidecar creates a cryptographically
random opaque ID, but callers that may need retry safety should supply and
retain their own stable, unique ID. Reusing one retained ID with the same local
target, project, and exact message returns the retained receipt with
`replayed: true` and does not spawn another native process. Reusing it with a
changed target, project, or message fails with `request_conflict`. A retained
pending reservation, including one left by a crash, replays as
`request_pending` with delivery unknown and must never be retried
automatically. Both human and JSON output report `request_id` and `replayed`.

Every new send is fail-closed behind a persistent audit reservation. Before
spawning, Sidecar writes and synchronizes a pending record to private
`audit.jsonl` mode `0600` in the private runtime directory. After the native
process returns, it writes a terminal receipt. The current log and one
`audit.jsonl.1` rotation are each bounded; the private `audit.key` is mode
`0600`. Records contain the opaque request ID, keyed request/target HMACs, and
receipt metadata. They do not store the message, response or other content,
unkeyed content or identity hashes, filesystem paths, native stdout, or native
stderr. Request-ID idempotency lasts only while the relevant records remain in
the current or rotated log.

The audit store binds each canonical runtime namespace and HMAC key to a
private anchor under the effective account's configured home, independently of
the mutable `HOME` environment value. Runtime and anchor directories must be
owned and private, and path traversal, symlink, inode, ownership, mode, or key
changes fail closed. Send therefore requires POSIX descriptor-relative,
no-follow, and file-lock support; it is unsupported and fails closed when
those primitives are unavailable.

Before spawning, `send` also acquires a nonblocking, hashed per-session POSIX
lock under that retained runtime namespace, repeats the direct scan under the
lock, and revalidates the target's identity, eligibility, and source freshness.
It rejects a concurrent Sidecar send or a target that changed or disappeared,
and holds the lock until the native process exits. An audit write failure
before spawn rejects the send without running the native process. If the
terminal audit write fails after spawn, the result is `audit_error` with
delivery unknown.

Mutating send also requires deterministic descendant containment. On Darwin,
Sidecar starts a gated supervisor, installs a `kqueue` monitor for root-process
fork and exit activity, revalidates the target, and only then releases the
native target to execute. A clean no-fork completion can be reported as
delivered. Any observed fork is conservatively reported as
`error_code: "cleanup_incomplete"` with delivery unknown, even if the child
work was synchronous, the native root exited zero, and a response was captured.
Never retry that result automatically.

If the required containment primitives are unsupported, send reports
`containment_unsupported` with delivery unknown before the native target
executes. There are no whole-process scans or unverified descendant kills;
cleanup is limited to the owned root/process group. These constraints are why
any fork remains delivery unknown rather than being treated as proven cleanup.

`send` does not use the daemon or remote inventory, and it does not signal an
existing native process. It starts another native process that resumes the
persisted session. The lock coordinates only Agent Sidecar sends; a separately
launched native interactive agent does not honor it and can still race with the
resume or write concurrent history. Status is inferred and can lag, so do not
send to a session that may still be open or active even when it is reported as
`waiting`.

Eligible targets are local, top-level `claude`, `codex`, or `cursor-cli`
sessions in `waiting` or `idle`. `working`, `dead`, child, sidechain, and remote
sessions are rejected, as are `cursor-ide`, `copilot`, `kimi`, and `dsh`.
Claude and Codex receive the native prompt on stdin. Cursor CLI necessarily
receives it in the child process argv.

Compatibility was rechecked on 2026-08-23 without relying on machine-specific
paths. Kimi Code 0.38 remains unsupported for send because resumed print mode
auto-approves permissions and exposes the prompt in argv. DSH 0.1.1-rc.2
remains unsupported because it provides neither session resume nor stdin
prompt transport.

The message must be nonblank UTF-8 without NUL and at most 16 KiB. Timeout is
1–900 seconds and defaults to 300. Execution is bounded and Agent Sidecar
attempts one resume. Timeout, native failure, or output overflow means delivery
is unknown; never retry an unknown delivery because the agent may already have
received or acted on the message.

On success, human output is the final native response, or a delivery receipt
when no response is available. `--json` emits one result object. Exit `0` means
the native resume completed successfully and delivery is reported as
`delivered`; exit `1` means runtime failure, timeout, or overflow with
`delivery: "unknown"`; exit `2` is a preflight or usage rejection before a
valid resume result; interruption exits `130` and delivery is unknown.

The positional message is present in the `agent-sidecar` command argv and may
be stored in shell history or visible in process listings. For Cursor CLI it is
also present in the native child argv. Do not use this command for secrets.

The only recovery command for a corrupt, unsafe, or replaced send-audit
namespace is:

```sh
agent-sidecar audit reset --allow-write --confirm CLEAR-SEND-AUDIT
```

Reset refuses to run while a send is using the audit namespace. It
irreversibly deletes the audit key and retained logs, so all audit and
request-ID idempotency history is lost. Run it only for an explicit user
request to perform that recovery; never use it as automatic error handling.

Open the live terminal dashboard, or render one ANSI-free snapshot:

```sh
agent-sidecar tui
agent-sidecar tui --once
```

Press `q` to leave the interactive dashboard.

Manage the optional per-user daemon:

```sh
agent-sidecar daemon start
agent-sidecar daemon start --http
agent-sidecar daemon start --http --http-port 43123
agent-sidecar daemon status
agent-sidecar daemon stop
agent-sidecar daemon run
agent-sidecar daemon run --http
```

`start` launches a detached daemon and is idempotent. `run` runs the same
daemon in the foreground, which is useful under a process supervisor. HTTP is
off by default and is enabled only by `--http`; `--http-port` requires
`--http` and accepts `0` through `65535`. Omitting it or selecting `0` chooses
an ephemeral port.

Background `start` waits until the daemon and requested HTTP listener report
ready. `start` and `daemon status` report the numeric-loopback URL and private
token-file path, never the token. Starting with HTTP flags that do not match an
already running daemon fails instead of silently changing its configuration.

### Persistent macOS user service

On macOS, explicitly install the daemon as a current-user LaunchAgent:

```sh
agent-sidecar service install [--http [--http-port PORT]] [--force]
agent-sidecar service install
agent-sidecar service install --http
agent-sidecar service install --http --http-port 43123
agent-sidecar service status
agent-sidecar service uninstall
```

Service installation is never automatic. It writes the validated user plist
at `~/Library/LaunchAgents/com.agent-sidecar.daemon.plist`, loads label
`com.agent-sidecar.daemon` in the `gui/<uid>` domain, and configures both
`RunAtLoad` and `KeepAlive`. The stored runtime command is cwd-independent for
pipx, executable zipapps, and the checkout shim. Service control is unsupported
on non-Darwin systems and fails without changing service state.

An identical install is idempotent. Changing a validated definition, including
HTTP mode, port, or runtime directory, is refused unless `--force` is supplied:

```sh
agent-sidecar service install --http --http-port 43123 --force
```

`--force` deliberately unloads and replaces the definition, causing an
interruption; rollback is attempted on failure but can itself be incomplete.
Use it only for an intentional configuration replacement. For a version
update, use the old command to uninstall first, update or replace the pipx
environment, zipapp, or checkout in place, and then reinstall with the desired
HTTP flags. Uninstall before changing the runtime command's path. `service
uninstall` removes the validated LaunchAgent and stops its daemon, but retains
the private runtime directory and its diagnostic and HTTP-token data. Any
send-audit files there are retained too, but are owned by the CLI `send`
workflow rather than daemon observation.

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

The default daemon has no network listener. Its runtime directory is created
with user-only permissions where supported, and the socket and PID file are
mode `0600`. The daemon refuses to replace unsafe non-socket/non-regular paths,
and `daemon stop` signals a process only after the socket PID and PID file
agree.

The socket uses bounded newline-delimited JSON requests and responses. Its
operations are `ping`, `status`, and `subscribe`: `status` returns the current
session snapshot plus scan and tailer diagnostics, while `subscribe`
acknowledges the request and then streams normalized event objects. User-facing
daemon consumers report bounded, sanitized tailer diagnostics on stderr.

The daemon also writes structured diagnostics to private `daemon.jsonl` in the
mode-`0700` runtime directory. The current mode-`0600` file is bounded to 2 MiB
and rotates through two mode-`0600` backups, `daemon.jsonl.1` and
`daemon.jsonl.2`. Records use an allowlisted schema of timestamps, component
and event names, stable error codes, bounded counts, and similar operational
metadata. They do not contain transcript or message content, responses,
filesystem paths, tokens, cookies, environment values, stdout, or stderr;
session identifiers are represented by short hashes.

Startup, readiness, shutdown, and bounded scan/tailer failures are logged.
Error and critical records, including tail errors, are synchronized durably.
An unsafe log, unsupported locking primitive, or later write failure disables
logging rather than stopping the daemon. A path-free stable code is emitted to
foreground stderr where available and included as a `daemon_log` `log_error`
entry in daemon status protocol/API diagnostics.

These daemon observation writes are distinct from `audit.jsonl`, `audit.key`,
send locks, and their namespace anchor, which are created and managed by CLI
`send`/`audit reset` operations. Merely starting or observing through the
daemon does not create send-audit state.

The daemon is optional. `list`, `status`, and `tui` prefer its snapshot and
fall back to direct read-only scans when it is unavailable. `watch --all`
prefers a daemon subscription for new events and falls back to direct
multi-session following. If an established daemon subscription is lost, the
CLI warns before switching to direct tailing because events during the
transition may be missed. A one-session watch is direct, and any `--from-start`
watch uses direct sources so it can replay existing records.

### Opt-in loopback HTTP

`daemon start --http` and `daemon run --http` add a read-only HTTP listener to
the normal Unix daemon. It binds only numeric `127.0.0.1` using IPv4: never
`localhost`, IPv6, a wildcard address, a LAN interface, or a remote host. One
daemon owns the runtime and one process owns the selected port. This is a local
convenience boundary, not a remote access or control plane.

HTTP remains default-off. When it is explicitly enabled, the mode-`0700`
runtime contains private mode-`0600` `http.token` and `http.port` files. The
token is retained for later HTTP starts; the instance-owned port record exists
only while that HTTP daemon is running and is removed on clean stop. Unsafe
runtime, token, or port paths, ownership, modes, links, or contents fail HTTP
startup. Stopping the daemon closes active clients and event streams, releases
the listener and runtime ownership, and removes only its own port record.

The token is never printed or logged and is never placed in a URL, cookie,
`localStorage`, or other browser storage. Open the URL reported by `daemon
start` or `daemon status`, then paste the token from the reported private file
into the panel. The panel keeps it only in page memory, clears the form field,
does not persist it, and retains at most 200 displayed events. Do not put the
token in command arguments or examples where shell history or process listings
could expose it.

The unauthenticated surface is the panel shell at `/` and the minimal
`GET /api/v1/health` response. `GET /api/v1/status` and the newline-delimited
JSON stream at `GET /api/v1/events` require the token as a bearer authorization
header. The adapter has no send, audit-reset, daemon-control, or other mutation
endpoint and sends no CORS allow headers. Every request requires the exact
numeric-loopback `Host`; a supplied `Origin` must match that same origin.
Request sizes and deadlines are bounded, with at most 16 HTTP clients and four
event streams.

## Agent skill integration

The installed Cursor and Claude skill links expose the same
`skills/agent-sidecar` bundle. The skill instructs agents to query
`agent-sidecar status --json` first for local observation, use remote monitoring
only on request, never hide remote watch failures or gap warnings, never
auto-retry a remote watch, and start or stop the daemon only when requested.
It may read `service status` when useful, but may install, force-replace, or
uninstall the LaunchAgent only in response to an explicit user request.
HTTP is started only on an explicit HTTP request; the skill reports its URL and
token-file path but never reads, echoes, or sends the token into chat. Existing
observation commands continue to use the Unix daemon by default. The skill may
run `send` only for an explicit same-turn request containing the exact message
or action; that request supplies the permission represented by `--allow-write`,
so no second confirmation is required. It never infers send consent from
monitoring, never retries an unknown delivery or pending request, and never
invokes audit reset automatically.

## Development

Install the development-only tools and run the canonical local quality gate
from the repository root:

```sh
python3 -m pip install -e '.[dev]'
ruff check .
python3 -m unittest tests.test_governance -v
python3 scripts/check.py
```

`scripts/check.py` runs Ruff, the complete standard-library test suite,
coverage policy, deterministic package smoke tests, CLI checks, and skill
checks in the same stable order used by CI. See [Contributing](CONTRIBUTING.md)
for branch, pull-request, review, changelog, and release governance.

### Release and version checklist

1. Set the intended version in `sidecar/__init__.py` and update documentation
   references that describe the current release; retain explicitly historical
   references.
2. Run `python3 scripts/check.py` and all release-specific checks.
3. Build `dist/agent-sidecar.pyz` twice from the same source and verify the
   reported SHA-256 values match.
4. Smoke-test both executable and `python3 -I` zipapp invocation from a
   directory outside the checkout, and verify a wheel exposes the
   `agent-sidecar` console script with no `Requires-Dist` metadata.
5. Follow the authoritative
   [release procedure](CONTRIBUTING.md#release-procedure): release from a
   CI-green `main` commit, fast-forward the `release` branch for a final
   release, create one immutable `v<version>` tag, and let the guarded GitHub
   workflow verify and publish the zipapp, checksums, and provenance. PyPI
   publication is not currently claimed by this documentation.

## Security and reporting

Read the [Security Policy](SECURITY.md) for supported security versions, trust
boundaries, safe diagnostic handling, and the private vulnerability-reporting
channel. Do not disclose suspected vulnerabilities in a public issue.

For non-security defects and feature requests, use the repository's
[issue forms](https://github.com/shendeguize/AgentSideCar/issues/new/choose).
Before sharing logs, JSON output, screenshots, transcripts, databases, or
runtime files, follow the sanitization requirements in the Security Policy.

## Current scope and deferred work

Version 0.4.1 provides local observation for the supported sources, Cursor CLI
event watching, remote `list`/`status` snapshots, concurrent local and remote
`watch --all --remote`, and experimental local send for Claude, Codex, and
Cursor CLI. It packages the CLI for pipx and deterministic zipapp use, and adds
explicit macOS LaunchAgent management plus private rotating daemon diagnostics.
Remote prefix watch and remote send remain unsupported. Kimi and DSH injection
remain deferred; Cursor IDE and Copilot send are unsupported. The opt-in HTTP
panel and read-only API remain numeric-IPv4-loopback-only and do not extend
remote monitoring or provide a control plane.

## FAQ

### Is Agent Sidecar published on PyPI?

No publication is claimed. Use `pipx` with a checkout or Git URL, or download a
verified executable zipapp from the
[GitHub Releases](https://github.com/shendeguize/AgentSideCar/releases) page.
Do not assume that an unqualified package from PyPI is this project.

### Can I use Agent Sidecar on Linux or Windows?

Linux supports the portable, read-oriented paths described in the
[support matrix](#support-matrix), with CI coverage and explicit exclusions
for the macOS LaunchAgent and experimental `send`. Windows is currently
unsupported because required POSIX security and daemon primitives are absent.

### Why can a completed session still appear working?

Status is inferred from persisted evidence rather than a native control-plane
signal. Some tools flush state lazily, so a completed turn—especially in Cursor
IDE—can remain `working` for several minutes before becoming `waiting`.

### Can Agent Sidecar control remote sessions?

No. Remote `list`, `status`, and `watch --all --remote` are observation-only.
Remote prefix watch and remote message delivery are unsupported. Experimental
`send` is local-only, limited to eligible sources, explicitly gated by
`--allow-write`, and available only where its containment contract is
supported.

### Where does Agent Sidecar store its own state?

The default runtime directory is `~/.agent_sidecar`. It can contain the Unix
socket, PID file, bounded diagnostics, HTTP token and transient port record,
and, only after mutating send operations, private audit state. See
[Uninstall](#uninstall) before removing anything; normal uninstall deliberately
retains security-relevant history.

## License

Agent Sidecar is released under the [MIT License](LICENSE).
