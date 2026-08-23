# Agent Sidecar Reference

## Installation and release artifact

Agent Sidecar 0.4.1 requires Python 3.9+ and declares no runtime Python
dependencies. It is not documented as published on PyPI. Install the console
script from a checkout or Git source:

```text
pipx install .
pipx install 'git+https://github.com/shendeguize/AgentSideCar.git'
```

Append an available release tag such as `@v0.4.1` to the Git URL when an
immutable revision is required. The existing `scripts/install-skill.sh`
alternative creates only symlinks into its checkout and retains its
idempotent/refuse-unrelated-path safety behavior.

Build a deterministic standalone artifact with:

```text
agent-sidecar package build --output dist/agent-sidecar.pyz
./dist/agent-sidecar.pyz --version
python3 -I dist/agent-sidecar.pyz --version
```

The build is atomic, bounded, mode `0755`, and reports the artifact path,
SHA-256, and size. It packages the active installed `sidecar` package: checkout
sources when invoked through a checkout, installed site-packages under pipx or
a wheel environment, and the embedded package when invoked from a zipapp. It
does not select source from the caller's current working directory. Identical
active package content produces identical bytes. Restore the executable bit
after a transfer before using the artifact for detached daemon or service
startup.

For pipx console scripts, executable zipapps, and the checkout shim, `daemon
start` resolves a stable absolute runtime command before detaching. It is
therefore independent of the caller's current working directory.

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
- `waiting`: a fresh turn appears complete and the session appears available
  for user input or review.
- `idle`: no sufficiently recent activity was observed.
- `dead`: a required local session source is missing or no longer readable.

Statuses are inferred from persisted local state. They are not commands sent to
the agent. Cursor IDE transcript and terminal updates can make `waiting` appear
several minutes late.

## Read-only storage behavior

Cursor CLI observation normally copies the bounded main database and WAL into a
private temporary snapshot before decoding. A metadata fallback may open the
live main database with SQLite `mode=ro&immutable=1`; it does not read the WAL
or take SQLite locks.

The default Unix daemon writes its socket and PID file, may create bounded
ephemeral private temporary snapshots, and persists bounded private
`daemon.jsonl` diagnostics. If and only if HTTP is explicitly enabled, the
daemon also retains private `http.token` and owns a transient `http.port`
record while that HTTP instance runs. These daemon observation writes make no
persistent transcript or agent-configuration changes.

Send-audit logs, keys, locks, and namespace anchors are separate CLI-owned
state. They are created or managed only by explicit CLI `send`/`audit reset`
operations, not by daemon observation, status, or HTTP startup. The resumed
native agent launched by `send` can still modify its own session and workspace
as documented below.

## Private daemon diagnostics

The mode-`0700` runtime directory contains these daemon-owned diagnostic files:

- `daemon.jsonl`: current structured diagnostic log, mode `0600`, at most
  2 MiB.
- `daemon.jsonl.1` and `daemon.jsonl.2`: up to two rotated mode-`0600`
  generations, each at most 2 MiB.
- `daemon.log.lock`: private mode-`0600` lifetime rotation lock.

The allowlisted JSONL schema contains timestamps, schema and Sidecar versions,
PID, component, event, level, stable error codes, bounded counts and HTTP
metadata, and selected adapter/agent/stage fields. Session IDs are represented
as short SHA-256-derived values. Records do not contain transcript, prompt,
message, response or other content; filesystem paths; tokens, cookies, auth or
environment values; native stdout or stderr; or raw exception text.

Startup, readiness, and shutdown events are recorded. Scan and tailer errors
are deduplicated and written with privacy-safe codes. Error/critical records,
including tail errors, are synchronized durably. Rotation and crash-suffix
repair preserve complete JSON lines.

Logging is diagnostic, not a daemon availability requirement. Unsafe log
objects, unsupported locking, or a write failure disable further logging
without stopping the daemon. The first failure is surfaced as a path-free
stable code on foreground stderr where available and as a `daemon_log`
`log_error` item in the daemon status protocol/API `diagnostics` list.

## Event JSON

`watch ... --json` emits newline-delimited event objects:

- `ts` (string): normalized event timestamp.
- `agent` (string): source adapter.
- `session_id` (string): source session identifier.
- `kind` (string): normalized event kind.
- `text` (string): normalized human-readable event text.
- `extra` (object): adapter-specific event metadata.

With `watch --all --remote`, every event also has `host` (string). `local`
identifies local provenance; remote events use their DSH Center inventory
alias. Human output prepends a fixed-width host column. Without `--remote`,
local event JSON and human output keep their original form.

Without `--from-start`, watch follows newly observed events. `watch --all`
prefers the daemon event stream and falls back to direct multi-session
observation when the daemon is unavailable. Daemon-backed commands report
bounded, sanitized tailer diagnostics on stderr. If an established daemon
subscription drops, `watch --all` warns before switching to direct tailing
because events during the transition may be missed.

## Opt-in loopback HTTP

```text
agent-sidecar daemon start --http
agent-sidecar daemon start --http --http-port 43123
agent-sidecar daemon run --http
```

HTTP is disabled by default. `--http-port` requires `--http` and accepts `0`
through `65535`; omitted or zero selects an ephemeral port. `start` waits for
the background daemon and requested listener to report ready before returning.
`start` and `daemon status` print the URL and token-file path, not the token.
An already running daemon must match the requested HTTP configuration.

The listener uses numeric IPv4 `127.0.0.1` only. It never binds IPv6, a
wildcard, LAN interface, remote host, or hostname alias. A lifetime runtime
lock permits one HTTP owner and the selected TCP port belongs to that one
process.

When HTTP is explicitly enabled, the mode-`0700` runtime contains:

- `http.token`: a retained private bearer token, regular file mode `0600`.
- `http.port`: transient current-port and instance-ownership metadata, regular
  file mode `0600`; clean shutdown removes only the running instance's record.

Unsafe runtime or metadata type, link, owner, mode, identity, or content fails
startup. `daemon stop` verifies the daemon, closes bounded HTTP clients and
event streams, releases the listener and runtime lock, and removes the owned
port record. The token remains private for reuse.

The token is never printed or logged and must never be put in a URL, cookie,
`localStorage`, other browser storage, command argument, or chat. The panel asks
the user to paste it from the reported private file, clears the form field
after connection, and holds the value only in page memory. It shows active
`working`/`waiting` sessions and retains at most 200 rendered event records.

The read-only HTTP routes are:

- unauthenticated `GET /`: browser panel shell only.
- unauthenticated `GET /api/v1/health`: minimal `{"ok":true}` health response.
- bearer-authenticated `GET /api/v1/status`: session snapshot plus scan and
  tailer diagnostics.
- bearer-authenticated `GET /api/v1/events`: NDJSON subscription
  acknowledgement followed by normalized event objects.

There are no send, audit-reset, daemon-control, or other mutation routes, and
no CORS allow headers. Every request must carry the exact
`127.0.0.1:<bound-port>` Host value. If Origin is present, it must be the exact
matching numeric-loopback origin. Requests, responses, and deadlines are
bounded; the listener permits at most 16 concurrent clients and four event
streams.

## Explicit macOS user service

```text
agent-sidecar service install [--http [--http-port PORT]] [--force]
agent-sidecar service install
agent-sidecar service install --http
agent-sidecar service install --http --http-port 43123
agent-sidecar service install --http --http-port 43123 --force
agent-sidecar service status
agent-sidecar service uninstall
```

Service control is supported only on macOS with `/bin/launchctl`; non-Darwin
platforms return an unsupported control error without installing anything.
Installation is explicit and never triggered by `daemon start` or any
observation command.

The service is a current-user LaunchAgent:

- label: `com.agent-sidecar.daemon`.
- domain: `gui/<effective-uid>`.
- definition:
  `~/Library/LaunchAgents/com.agent-sidecar.daemon.plist`.
- lifecycle: `RunAtLoad=true`, `KeepAlive=true`, background process with a
  bounded launchd throttle.
- command: the cwd-independent pipx console script, executable zipapp, or
  checkout shim resolved at installation time, followed by `daemon run`.

An identical install is idempotent. A different validated definition, such as
a changed HTTP mode, port, or runtime directory, is rejected unless `--force`
is present. Force unloads the existing job, atomically replaces the definition,
bootstraps it, and waits for daemon readiness. It interrupts service and,
although rollback is attempted, a failure can report incomplete rollback. It
must not be used without an explicit user request for the replacement.

For version updates, run `service uninstall` through the old runtime before
upgrading, replacing, or moving that runtime, then run `service install` from
the new version with the intended HTTP flags. This ensures the process reloads
and prevents a retained plist from pointing to a missing command.

Uninstall boots out only the validated managed job and removes its plist. It
does not purge the private Agent Sidecar runtime directory or retained audit,
HTTP-token, and daemon-log data. `service status` is read-only; install, force,
and uninstall are mutating and require an explicit user request.

## Process JSON

`ps --json` emits a JSON array with `pid`, `etime`, `exe`, `cwd`, and `cmd`.
Process presence is supporting evidence only; it does not map reliably to one
persisted session.

## Remote snapshots

`list --remote` and `status --remote` merge local sessions with snapshots from
eligible DSH Center hosts. `--host <host-alias>` is repeatable and
case-insensitive, requires `--remote`, and limits only the remote targets.
Human output adds a `HOST` column. Remote `list` applies the same 48-hour
default as local `list`; `--all` requests the full available history.

Inventory comes from `dshc ls --json`, or from the DSH Center config and state
files under `DSHC_HOME` or `~/.dsh_center` when that command is unavailable.
The query uses a bounded deterministic zipapp built from the active installed
`sidecar` package and streamed over strict, noninteractive SSH. This means
checkout source, installed site-packages, or an embedded zipapp package as
applicable, not an assumed current checkout. Remote hosts need Python 3.9+ but
do not need Agent Sidecar or third-party Python packages installed. Host keys
must already be trusted and noninteractive authentication must already work.

Host failures are isolated and reported on stderr using one of `host_key`,
`auth`, `timeout`, `unreachable`, `python_too_old`, `resource_limit`,
`protocol`, or `remote`. `resource_limit` identifies a bounded-data violation.
Successful and local rows remain available during partial failure.

- Exit `0`: at least one requested remote host succeeded, including a partial
  fleet success, or the eligible fleet was empty. The empty case reports a
  notice that only local sessions are shown.
- Exit `2`: invalid inventory, setup, selection, or command usage. Local rows
  are still emitted when available.
- Exit `3`: a nonempty requested fleet had zero successful hosts. Local rows
  are still emitted when available.

Remote snapshots do not include `tui`, daemon aggregation, or `send`.

## Remote watch

```text
agent-sidecar watch --all --remote
agent-sidecar watch --all --remote --host <host-alias>
agent-sidecar watch --all --remote --from-start --json
```

Remote watch requires `--all` and follows local sessions and eligible remote
hosts concurrently. `--host <host-alias>` is repeatable, case-insensitive, and
limits only the remote hosts; local observation remains enabled. `--from-start`
replays available history on both sides before following new events. A remote
session prefix is unsupported.

Inventory and eligibility match remote snapshots: DSH Center hosts must be
enabled, nonlocal, non-orphaned, and in the `ready` or `no_dsh` phase. Each
selected host must provide Python 3.9 or newer. Agent Sidecar uses strict,
noninteractive SSH, streams a bounded deterministic zipapp built from the
active installed `sidecar` package, writes it to a private temporary remote
file, preflights and runs it in isolated Python mode, then removes it during
cleanup. The active package is checkout source, installed site-packages, or an
embedded zipapp package as applicable; nothing is installed remotely.

Local and remote producers run concurrently. Their handoff queues and the
fleet's per-host/global event buffer are bounded; full queues backpressure
producers rather than dropping queued events. Round-robin admission and
draining keep a busy host from starving other hosts or the local source.

After the remote Python and zipapp preflights, a host is reported ready when
the watch child emits its first valid event or remains alive for a one-second
readiness grace. The child has no internal ready frame, so a silent
initialization failure after the grace can still arrive after readiness.

There is no automatic reconnect. Any terminal host failure emits a sanitized,
stable failure code on stderr plus `events may be missed`; local and successful
peer streams continue. A lost local daemon subscription emits a separate
transition-gap warning before direct local fallback. Callers must surface these
warnings and must not retry automatically.

`Ctrl-C` shares cancellation across local and remote producers, closes SSH
process groups, joins workers with bounded deadlines, removes remote temporary
files, and exits `130`.

- Exit `0`: normal completion, or a partial remote failure with a usable local
  source or successful remote stream.
- Exit `1`: the eligible fleet is empty and no usable local source exists, or
  another local/output runtime failure.
- Exit `2`: invalid usage, inventory, setup, or host selection.
- Exit `3`: every host in a nonempty remote fleet failed and no usable local
  source exists.
- Exit `130`: interrupted by the user.

An empty eligible fleet prints `remote: no eligible hosts; showing local
sessions only`. The command continues local observation and returns `0` when a
usable local source exists; otherwise it exits `1`. Remote prefix watch and
remote `send` are unsupported.

## Local send

`send` is an experimental mutation, not an observation command:

```text
agent-sidecar send <session-prefix> "<message>" --allow-write
agent-sidecar send <session-prefix> "<message>" --allow-write --timeout 120 --json
agent-sidecar send <session-prefix> "<message>" --allow-write --request-id <stable-unique-id> --json
```

The resolver prefers a sole exact session ID; otherwise the prefix must match
exactly one discovered local session. The target must be a top-level
`waiting` or `idle` session from `claude`, `codex`, or `cursor-cli`. Remote,
`working`, `dead`, child, and sidechain sessions are rejected. `cursor-ide`,
`copilot`, `kimi`, and `dsh` are unsupported.

Compatibility was rechecked on 2026-08-23 without recording local executable
paths. Kimi Code 0.38 remains unsupported for send because resumed print mode
auto-approves permissions and exposes the prompt in argv. DSH 0.1.1-rc.2
remains unsupported because it provides neither session resume nor stdin
prompt transport.

`send` first scans local state to resolve the target. Before spawning a
separate blocking native headless-resume process, it opens the retained audit
namespace, writes a pending reservation, acquires a nonblocking, hashed
per-session POSIX lock, performs a fresh direct scan under the lock, and
revalidates the exact identity, eligibility, executable, project, and source
signature. It rejects a concurrent Sidecar send or a target that changed or
disappeared, and holds the lock until the native process exits.

The lock files live under `send-locks` in `AGENT_SIDECAR_RUNTIME_DIR`, or under
`~/.agent_sidecar/send-locks` by default. The runtime and lock directories are
mode `0700`, lock files are mode `0600`, and lock filenames do not expose
session IDs.

`send` does not use the daemon or remote inventory, and it does not signal an
existing native process. It resumes the persisted session in another native
process. The lock coordinates only Agent Sidecar sends; a separately launched
native interactive agent does not honor it and can still race with the resume
or write concurrent history. Because inferred `waiting` and `idle` state can
lag reality, do not send to a session that may still be open or active even if
it is reported as `waiting`.

The native process may contact its provider, run tools, and modify agent-owned
state. Claude and Codex receive the native prompt on stdin; Cursor CLI
necessarily receives it in the child argv.

`--allow-write` is mandatory. The message must be nonblank valid Unicode, must
not contain NUL, and must be at most 16 KiB after UTF-8 encoding. `--timeout`
accepts 1 through 900 seconds and defaults to 300. One native resume is
attempted; there is no retry.

### Deterministic descendant containment

Mutating send requires deterministic descendant containment in addition to the
session lock and audit reservation. On Darwin, Sidecar first starts a private
gated supervisor rather than the native target directly. It attaches a kernel
`kqueue` process monitor for root fork and exit activity, performs the final
target revalidation, and only then opens the gate so the supervisor can execute
the native target.

A native result can be reported as delivered only when the monitored root
completes successfully with no observed fork and containment remains reliable.
Any fork permanently makes cleanup unprovable for that send. The result is
therefore `error_code: "cleanup_incomplete"` and `delivery: "unknown"` even if
every child completed synchronously, the root returned zero, and bounded
response text was captured. An otherwise completed run becomes
`outcome: "failed"`; a timed-out run remains `outcome: "timed_out"` while using
the same cleanup error code. Never retry `cleanup_incomplete` automatically.

If Darwin `kqueue` fork monitoring or another required containment primitive is
unavailable, the result is `outcome: "failed"`,
`error_code: "containment_unsupported"`, and `delivery: "unknown"`; the native
target is not executed. Sidecar does not scan the whole process table for
descendants and does not kill candidate descendant PIDs without verified
ownership. There are no whole-process scans or unverified descendant kills.
Cleanup signals only the root or process group that Sidecar created and owns.

This containment contract does not coordinate with a separately launched
native interactive agent. That process can still race with the resume or write
concurrent history, so the residual native-agent concurrency warning still
applies.

### Request ID and audit behavior

`--request-id` is an optional opaque idempotency key using conservative ASCII
and at most 128 bytes. If omitted, Sidecar generates a cryptographically random
ID. A caller that might safely repeat an invocation should instead supply and
retain a stable, unique ID.

While its records are retained, the same request ID bound to the same agent and
session target, project, and exact message returns the latest receipt with
`replayed: true` and never spawns again. Changing the target, project, or
message while reusing that ID fails before spawn with `request_conflict`. A
pending record left by a crash returns `outcome: "request_pending"`,
`delivery: "unknown"`, and `replayed: true`; never retry it automatically.

Every new request is reserved before spawn in a mandatory persistent send-audit
store. The private runtime contains mode-`0600` `audit.jsonl`, a single bounded
mode-`0600` rotation at `audit.jsonl.1`, and a mode-`0600` `audit.key`. Each log
is bounded to 8 MiB. A pending record is synchronized before spawn and a
terminal receipt is synchronized afterward.

Audit records contain the opaque `request_id`, keyed request and target HMACs,
and bounded receipt metadata such as agent, timestamp, outcome, delivery,
error code, and return code. They do not contain the message, native response
or other content, unkeyed content or identity hashes, filesystem paths, stdout,
or stderr. Replayed results consequently do not reconstruct response or
diagnostic content. Request-ID idempotency is available only while the
request's records remain in `audit.jsonl` or `audit.jsonl.1`; rotation can
eventually expire it.

The audit namespace uses a private anchor under the effective account's
configured home, independent of the mutable `HOME` environment value, to bind
the canonical runtime namespace, runtime inode, audit-key inode, and key
fingerprint for an audit epoch. Runtime and anchor paths are traversed with
descriptor-relative no-follow checks; unsafe symlinks, replacements,
ownership, modes, or key changes fail closed. These guarantees require POSIX
directory-descriptor and file-lock primitives. Send is unsupported and fails
closed where they are unavailable.

If audit reservation or synchronization fails before native spawn, the send is
rejected and no native process runs. If terminal audit persistence fails after
the native process ran, the result is `outcome: "audit_error"` and
`delivery: "unknown"`.

The message is present in the sidecar command argv and can be recorded in shell
history or process listings. Cursor CLI also exposes it in its native child
argv. Do not send secrets.

With `--json`, stdout is one object with exactly these fields:

- `agent`: selected native agent.
- `session_id`: full selected session ID.
- `outcome`: `completed`, `failed`, `timed_out`, `overflow`,
  `request_pending`, or `audit_error`.
- `delivery`: `delivered` only for native success; otherwise `unknown`.
- `returncode`: native exit status when available, otherwise null.
- `response`: bounded parsed native response text.
- `stderr`: bounded native diagnostic text.
- `error_code`: stable failure code or null.
- `request_id`: caller-supplied or generated opaque request ID.
- `replayed`: whether the result came from retained audit history without a
  native spawn.

Without `--json`, every result starts with `request_id=<id> replayed=<bool>`;
success then prints the response or a delivery receipt. Runtime failure prints
a delivery-unknown receipt and diagnostics. Output is bounded: message input is
16 KiB, response capture is 4 MiB, and stderr capture is 64 KiB.

- Exit `0`: `outcome: "completed"` and `delivery: "delivered"`.
- Exit `1`: `failed`, `timed_out`, `overflow`, `request_pending`, or
  `audit_error`; delivery is unknown.
- Exit `2`: usage, prefix resolution, target eligibility, or other preflight
  rejection before a valid result, including `request_conflict` and
  pre-spawn audit failure.
- Exit `130`: interrupted; delivery is unknown.

Never retry any result with `delivery: "unknown"`; the native agent may already
have received or acted on the message.

### Audit reset

The only recovery for a corrupt, unsafe, or replaced send-audit namespace is:

```text
agent-sidecar audit reset --allow-write --confirm CLEAR-SEND-AUDIT
```

Reset takes exclusive nonblocking audit locks and refuses to run while a send
is active. It irreversibly removes the retained logs, key, and namespace
binding, losing all audit and request-ID idempotency history. Use it only for an
explicit user request for that destructive recovery. Skills and callers must
never invoke reset automatically after an audit error.

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
agent-sidecar watch --all --remote --json
agent-sidecar watch --all --remote --host <host-alias> --json
agent-sidecar watch --all --remote --from-start --json
agent-sidecar send <session-prefix> "<message>" --allow-write
agent-sidecar send <session-prefix> "<message>" --allow-write --request-id <stable-unique-id> --json
agent-sidecar audit reset --allow-write --confirm CLEAR-SEND-AUDIT
agent-sidecar package build --output dist/agent-sidecar.pyz
agent-sidecar tui
agent-sidecar tui --once
agent-sidecar daemon start
agent-sidecar daemon start --http
agent-sidecar daemon start --http --http-port 43123
agent-sidecar daemon status
agent-sidecar daemon stop
agent-sidecar daemon run
agent-sidecar daemon run --http
agent-sidecar service install
agent-sidecar service install --http
agent-sidecar service install --http --http-port 43123
agent-sidecar service status
agent-sidecar service uninstall
```

Start the daemon only for a user-requested continuous monitor or TUI, or on an
explicit start request. Enable HTTP only on an explicit HTTP request; report its
URL and token-file path, but never read or expose the token. Stop it only on
request. `service status` is read-only and may be queried when relevant;
service install, force replacement, and uninstall require an explicit user
request and are never automatic. Existing observation commands retain the
Unix-daemon default and are readonly with respect to agent-owned stores. Run
remote monitoring only when the user explicitly requests it. Surface remote
watch failure and gap warnings, and never auto-retry a remote watch. Run `send`
only for an explicit same-turn request for that exact message or action. Never
retry unknown delivery, and never run audit reset automatically.
