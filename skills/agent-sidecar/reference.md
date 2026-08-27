# Agent Sidecar Reference

## Installation and release artifact

Agent Sidecar 0.8.0 requires Python 3.9+ for local installation and tooling and
declares no runtime Python dependencies. Its remote observation payload accepts
Python 3.8+ on SSH targets. It is not documented as published on PyPI. Install
the console script from a checkout or Git source:

```text
pipx install .
pipx install 'git+https://github.com/shendeguize/AgentSideCar.git'
```

Append an available release tag such as `@v0.8.0` to the Git URL when an
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
state. They are created or managed only by explicit CLI `send`/`audit rebind`/
`audit reset`
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

## Daemon Unix-socket protocol

The daemon socket speaks bounded newline-delimited JSON requests and
responses. Its operations are `ping`, `status`, `subscribe`, and `replay`.
`ping` returns the daemon PID, version, and HTTP self-description. `status`
returns the current session snapshot plus scan and tailer diagnostics.

`subscribe` acknowledges the request and then streams normalized event
objects on the same connection. The request may carry an optional nonempty
`agents` array of agent names; the daemon then streams only events from those
agents and echoes the sorted allowlist in its acknowledgement. Omitting
`agents` keeps the full stream. Filtered-out events never consume the
subscriber's bounded queue. An empty or non-string `agents` value fails with
`invalid_request`.

`replay {"session_id": ..., "after_seq": ..., "limit": ...}` returns one
bounded page of a session's historical normalized events whose `seq` cursor
is greater than `after_seq`. `session_id` is a required nonempty string.
`after_seq` is a nonnegative integer defaulting to `0`, meaning from the
start of the retained transcript. `limit` accepts `1` through `1024` records
per page and defaults to `256`. The response carries the normalized `events`,
their `count`, the highest observed `last_seq` cursor, and a `truncated`
flag.

`truncated` is true whenever more retained events may remain after the
returned cursor: the page reached `limit`, or the adapter's own bounded
decode stopped the page early on its byte or time budget while making cursor
progress. Keep paging with the returned `last_seq` as the next `after_seq`
until a page reports `truncated: false` at the true end of the retained
transcript. An early-stopped page without cursor progress reports
`truncated: false` because re-requesting the same page cannot retrieve more.

The replay data source is the session adapter's own bounded local-transcript
replay — currently only `dsh` sessions provide one — so it returns only
events that are still retained in that transcript and carry a `seq` cursor.
It cannot recover events the source never persisted or that exceed the
bounded decode. Sessions of other agents report `replay_unsupported`, a
session absent from the current snapshot reports `unknown_session`, malformed
arguments report `invalid_request`, and an adapter decode failure reports
`replay_failed`. Events dropped from a slow subscriber's bounded live queue
can be backfilled through `replay` only while they remain in the retained
transcript.

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

```text
agent-sidecar list --remote --remote-python /usr/bin/python3.11 --json
agent-sidecar status --remote --remote-python /usr/bin/python3.11 --json
```

`list --remote` and `status --remote` merge local sessions with snapshots from
eligible DSH Center hosts. `--host <host-alias>` is repeatable and
case-insensitive, requires `--remote`, and limits only the remote targets.
Human output adds a `HOST` column. Remote `list` applies the same 48-hour
default as local `list`; `--all` requests the full available history.

`--remote-python <absolute-path>` requires `--remote` and pins the same path on
every selected host for the invocation. It is fleet-wide; no per-host
interpreter configuration exists. `AGENT_SIDECAR_REMOTE_PYTHON` supplies the
same fleet-wide value only when the option is absent. Strict selection
precedence is CLI option, environment variable, then bounded default
candidates. The environment variable is read only for remote-enabled `list`,
`status`, and `watch`; all other invocations ignore it.

Inventory comes from `dshc ls --json`, or from the DSH Center config and state
files under `DSHC_HOME` or `~/.dsh_center` when that command is unavailable.

The C1–C4 cross-repository inventory contract is intentionally narrow. C1
accepts a bare HostView array or a `{"hosts": [...]}` container. Sidecar
consumes only `name`, `config.enabled`, `config.local`, `local`, `orphaned`,
and `phase`; extra HostView fields are ignored. A host is eligible only when it
is enabled, non-local, non-orphaned, and in the `ready` or `no_dsh` phase.
Map-form host containers are supported for the C4 config/state-file fallback,
not as the C1 producer shape. C5 tunneled plugin-page shape is tracked through
the cross-repository integration issue template.

The query uses a bounded deterministic zipapp built from the active installed
`sidecar` package and streamed over strict, noninteractive SSH. This means
checkout source, installed site-packages, or an embedded zipapp package as
applicable, not an assumed current checkout. Before transfer, the probe tries
exactly `python3`, `python3.14`, `python3.13`, `python3.12`, `python3.11`,
`python3.10`, `python3.9`, and `python3.8`, in that order. It selects the first
available candidate running Python 3.8 or newer. Candidate names resolve with
the `PATH` visible to the remote noninteractive SSH shell; that `PATH` may
differ from an interactive login's.

The probe uses `sh -c` only to give its inner candidate loop fixed POSIX
syntax. The outer command string remains subject to parsing by the remote login
shell. The existing multiline bootstrap already establishes this boundary, so
this is not a shell-independence guarantee. Under bounded default discovery,
the validated executable path returned by the qualifying interpreter is passed
to the bootstrap only within that same host session. Every invocation probes
afresh; no result or path is cached or persisted locally or remotely. If the
bounded candidates are exhausted, the host reports the existing
`python_too_old` code. The error-code set is unchanged. Remote hosts do not need
Agent Sidecar or third-party Python packages installed. Host keys must already
be trusted and noninteractive authentication must already work.

An explicit value must be a nonempty absolute path of at most 1024 characters,
contain only `[A-Za-z0-9._+/-]`, and have no `..` path segment. Invalid option
or environment values are rejected locally with exit `2` before any SSH
connection. A valid path replaces the defaults with a one-element probe
sequence. It is passed as a separate argv token to the unchanged fixed probe
script and bootstrap command, never inserted into script text. The bootstrap
uses that operator token verbatim. All probe response fields remain validated,
but a differing returned executable cannot replace the explicit pin. If that
path is missing, non-executable, or reports Python older than 3.8 on a host,
the host reports `python_too_old`; no default candidate or bare `python3`
fallback is attempted.

Host failures are isolated and reported on stderr using one of `host_key`,
`auth`, `timeout`, `unreachable`, `python_too_old`, `resource_limit`,
`protocol`, or `remote`. `resource_limit` identifies a bounded-data violation.
Successful and local rows remain available during partial failure.

After all per-host `python_too_old` lines, the CLI emits at most one aggregate
hint generated locally. Default-candidate exhaustion uses
`remote: no Python >= 3.8 found among bounded candidates on {n} host(s); use
--remote-python <absolute-path> or AGENT_SIDECAR_REMOTE_PYTHON to pin an
interpreter`. An unsatisfied explicit option or environment pin uses
`remote: the interpreter set via
--remote-python/AGENT_SIDECAR_REMOTE_PYTHON is missing or older than 3.8 on {n}
host(s)`.

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
agent-sidecar watch --all --remote --remote-python /usr/bin/python3.11 --json
```

Remote watch requires `--all` and follows local sessions and eligible remote
hosts concurrently. `--host <host-alias>` is repeatable, case-insensitive, and
limits only the remote hosts; local observation remains enabled. `--from-start`
replays available history on both sides before following new events. A remote
session prefix is unsupported. Its `--remote-python` option, environment
precedence, fleet-wide scope, local validation, one-element probe, fail-closed
errors, and aggregate hint are identical to remote snapshots.

Inventory and eligibility match remote snapshots: DSH Center hosts must be
enabled, nonlocal, non-orphaned, and in the `ready` or `no_dsh` phase. Each
selected host uses the remote-snapshot probe contract: `python3` first, then
`python3.14` down through `python3.8`, selecting the first available Python
3.8+ interpreter from the noninteractive SSH shell's `PATH`. The selected
executable path from bounded default discovery is bound only to the bootstrap
in that host session; an explicit pin instead retains the exact operator token
as described above. Each invocation probes again without a local or remote
cache. `sh -c` fixes only the inner POSIX syntax, while the remote login shell
still parses the outer command and the existing multiline bootstrap. Candidate
exhaustion remains `python_too_old`. Agent Sidecar uses strict, noninteractive
SSH, streams a bounded deterministic zipapp built from the active installed
`sidecar` package, writes it to a private temporary remote file, preflights and
runs it
in isolated Python mode, then removes it during cleanup. The active package is
checkout source, installed site-packages, or an embedded zipapp package as
applicable; nothing is installed remotely.

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

## DSH plugin injection and timeline contract

Inside DSH, injection is owned by
`@shendeguize/dsh-agent-sidecar` at
`/plugins/agent-sidecar/api/action`, not by a direct CLI send to a `dsh`
session. `inject.enabled` is off by default. Every enabled injection uses
`inject.prepare`, a user confirmation over the returned one-time token, and
then exactly one `inject.execute`; there is no bulk or scheduled injection.

The plugin computes and serves a per-target `inject_eligibility` verdict:

- `{allowed: true, reason: "eligible"}` is the only open state.
- Local DSH targets may be eligible in `working`, `waiting`, or `idle`.
- Local external `claude`, `codex`, `cursor-cli`, and `kimi` targets must be
  top-level, non-sidechain, and `waiting` or `idle`; `working` is rejected.
- Kimi follows the external protected ACP resume path and has no DSH live
  queue/steer path. Child, remote, dead, and malformed Kimi targets are
  invalid.
- DSH deliberately defers child/sidechain topology to its live/cold preflight;
  the external-agent top-level rule must not be projected onto DSH.
- Remote, dead, malformed, and unsupported-agent targets are rejected with a
  stable reason.

Clients must consume that host verdict rather than deriving eligibility from a
status badge. The server re-reads and revalidates the same target at both
prepare and execute, so eligibility can close between the two phases.

For a DSH target already present in `ctx.agents`, the plugin preserves the
live Agent's route and preset. `queue` synchronously calls `followup` for that
Agent's next turn; `steer` synchronously calls `steer` for its nearest
in-progress step. A successful splice means the live inbox accepted or queued
the message, not that a model turn or tool run completed.

This live path is the narrow reason a `working` DSH session can be eligible for
steer. It does not make an external working session writable. If no live DSH
Agent exists, the plugin enters a bounded cold-resume path before applying the
selected inbox operation. Persisted `waiting`, `idle`, and cold/no-status
sessions need no session-selected route. Cold resume requires all of the
following:

- the persisted target still exists;
- the host `agents`/persistence and preset-inspection services are available
  and remain in the same service generation through publication;
- the current host-default provider and model are both nonblank;
- neither the persisted effective state nor the host supplies an explicit or
  implicit preset.

Cold `steer` therefore does not mean steering a previously running turn. The
plugin first resumes an unpublished Agent under those checks and only then
calls `steer`. A preset returns `dsh_preset_unsupported`; an incomplete
default model route returns `dsh_model_unconfigured`; an unverifiable or
changing publication boundary returns `executor_error`.

Direct CLI send does not support a `dsh` target. A successful live/cold DSH
splice reports inbox acceptance only; consumers must observe the timeline and
must not reinterpret `delivery: "delivered"` as model-turn success.

The plugin's nested-modal isolation owns only the `inert` and `aria-hidden`
attributes it wrote. It drains queued lifecycle records at HMR handoff and
uses the exact current opener after a remove/reinsert race instead of restoring
stale focus. Final content-free real acceptance records Kimi protected-resume
PASS and DSH live-steer/UI PASS; it deliberately excludes private paths,
session IDs, transcripts, prompts, and hashes.

Injection HTTP status has these stable meanings:

- `404` with `target_not_found`: the target is absent at the server's current
  prepare/execute recheck. No target should be inferred from stale board data.
- `409`: a known target, token, or DSH cold-resume state conflicts with the
  requested operation. Reasons include external `working_session`,
  `dead_session`/`target_dead`, `dsh_model_unconfigured`,
  `dsh_preset_unsupported`, and execute-time token reuse or mismatch.
- `502`: fail-closed host-service or executor failure, normally
  `executor_error`, including a DSH state that cannot be inspected or safely
  published. No private upstream detail is part of the contract.

These statuses are not delivery receipts. In particular, an execute result
with `outcome: "unknown"` is returned as HTTP `200` because it is a terminal
delivery fact; the target may already have acted. Never retry that result
automatically. A DSH `outcome: "delivered"` means only that the synchronous
inbox splice returned successfully.

The plugin's `GET session/<session-id>` and
`GET session/<session-id>/timeline?cursor=&limit=` timeline bodies include the
legacy contribution booleans in `sources` plus this content-free health tuple:

```json
{
  "sourceOutcomes": {
    "liveSession": "succeeded",
    "sessionQuery": "unavailable",
    "sidecarReplay": "replay_unsupported",
    "buffer": "not_found"
  },
  "degraded": true,
  "reason": "partial_source_failure"
}
```

The four source outcome values are independently one of `succeeded`,
`unavailable`, `not_found`, `replay_unsupported`, or `source_failed`.
`replay_unsupported` and `source_failed` are failures. If at least one source
failed, `degraded` is true and `reason` is `partial_source_failure`, except
that an empty page whose usable sources all failed uses `all_sources_failed`.
A healthy page uses `degraded: false` and `reason: null`.

`unavailable` and `not_found` are absence signals, not upstream failures. A
known session stays HTTP `200` even when every usable source failed and the
page is empty; only a target unknown to both board/fusion state and all
consulted sources is `404`. Consumers must retain surviving entries, surface
degradation, and must not expose raw errors, paths, IDs, prompts, or other
upstream detail. Older hosts can omit the complete tuple; a partial tuple or
unknown enum must fail closed as unverified instead of being guessed.

## Local send

`send` is an experimental mutation, not an observation command:

```text
agent-sidecar send <session-prefix> "<message>" --allow-write
printf '%s' '<exact-message>' | agent-sidecar send '<full-session-id>' --agent '<agent-name>' --exact-session --message-stdin --allow-write --request-id '<unique-request-id>' --json
```

The resolver prefers a sole exact session ID; otherwise the prefix must match
exactly one discovered local session. `--agent` optionally filters resolution
to one exact agent name, case-insensitively. `--exact-session` optionally
requires the full session ID and disables prefix fallback. Integrations that
write should always use both options with `--message-stdin`, `--allow-write`,
a caller-retained `--request-id`, and `--json`, as in the second example.

The target must be a top-level `waiting` or `idle` session from `claude`,
`codex`, `cursor-cli`, or `kimi`. Remote, `working`, `dead`, child, and
sidechain sessions are rejected. `cursor-ide`, `copilot`, and `dsh` are
unsupported. Claude, Codex, and Cursor retain their prior resume and result
semantics; Kimi uses the special contract below.

`send` resolves against its direct local scan only. A session row obtained from
`list --remote` is not a send target; if its ID is absent from that local scan,
the command fails closed with exit `2` and JSON code `target_not_found`. The
`remote_session` rejection applies when a row found by the local scanner carries
remote provenance.

### Kimi Code 0.38.0 protected ACP resume

Kimi support is exact-version and manual. Use the full composite binding and
stdin message transport:

```sh
printf '%s' '<exact-message>' | agent-sidecar send '<full-kimi-session-id>' --agent kimi --exact-session --message-stdin --allow-write --request-id '<unique-request-id>' --json
```

The plan binds the selected session and project to Kimi's owner-safe,
descriptor-anchored identity evidence: Kimi home, index row, state file,
session directory, project sources, main-agent home, root identity, and root
`wire.jsonl`. The package distribution is copied into a private plan-bound
snapshot. For the Node distribution that snapshot also binds the Node
executable and non-system dylib closure. The implementation reprobes Kimi
version and ACP initialization from the snapshot and rejects any other Kimi or
matching Node owner process in the project. The owner guard uses bounded token
parsing for canonical Node file arguments, including supported
preload/import/loader values and later script arguments. Arbitrary long
ordinary Node commands are not Kimi candidates. The executable binding is
owner-safe and requires exactly one link; within that boundary, an ordinary
relative Node command whose cwd is deleted or inaccessible is skipped. Direct
Kimi hints and executable identity matches remain blockers; malformed or
over-budget input carrying that evidence and unsafe or changed executable
identity fail closed.

One bounded `kimi acp` process initializes with empty client capabilities,
matches the listed session and project, resumes with `mcpServers: []`, sets
Kimi's `default` mode, and writes one text prompt. Permission reverse requests
are answered `cancelled`; any question/approval path is cancelled by failing
closed, and unsupported reverse methods end the run. No MCP, filesystem, or
terminal capability is advertised. The message is present only in the ACP
`session/prompt` frame, not in Sidecar's or native Kimi's argv.

This path does not attach to the live terminal and has no inbox, queue, or
steer operation. A Kimi session observed as `working` is rejected. Child,
remote, dead, and malformed Kimi sessions are invalid.

Darwin containment may report `cleanup_incomplete` after observing a fork. If
ACP still returns rc `0`, settles at `end_turn`, and strict durable root-wire
plus state checks prove the exact matching completed turn, the public result is
`outcome: "completed"` with `delivery: "unknown"` and CLI exit `1`. It is
never delivered. Without that proof the public result is `failed` (or the
bounded timeout/overflow outcome) and unknown. This is evidence that the
protected resume completed, not a cryptographic request-to-provider-turn
binding. Never retry the same content under a new request ID. The same retained
request ID can only replay the stored audit result with `replayed: true`,
without another spawn.

Direct CLI send does not support DSH; DSH injection is available only through
the plugin contract above.

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
state. `--message-stdin` reads strict UTF-8 bytes from redirected or piped
standard input, keeps the message out of the Sidecar argv, and is mutually
exclusive with the positional message. Interactive terminal input is refused
instead of waiting. Input is exact: a trailing newline changes the message and
audit fingerprint, so use `printf '%s'`. Claude and Codex receive the native
prompt on stdin; Cursor CLI necessarily receives it in the child argv. Kimi
receives it in the ACP prompt frame and does not expose it in native argv.

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
the same cleanup error code. Kimi is the narrow exception to the outcome
mapping: strict durable proof can produce `completed`, but delivery remains
unknown and never delivered. Never retry `cleanup_incomplete` automatically.

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

Callers must bind every JSON receipt back to the request. Its `agent`, full
`session_id`, and `request_id` must exactly equal the requested triple.
Missing or mismatched identity fields, or exit `0` without a valid bound
receipt, are terminal delivery-unknown results. Other output, response text,
or a zero exit status cannot override that check.

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

The runtime binding and marker are persistent security state. Never delete,
move, recreate, or replace `AGENT_SIDECAR_RUNTIME_DIR`, `audit.key`, retained
logs, or the home-anchor marker to bypass `unsafe_lock`. The code means the
old namespace identity or history cannot be proved; deleting the marker cannot
make its request retry-safe. Preserve the evidence and fail closed. A new
owner-private runtime path and new request ID are valid only for an explicitly
new request lineage, not recovery or replay of the old one, and its audit
history must be retained thereafter.

If audit reservation or synchronization fails before native spawn, the send is
rejected and no native process runs. If terminal audit persistence fails after
the native process ran, the result is `outcome: "audit_error"` and
`delivery: "unknown"`.

A positional message is present in the Sidecar command argv and can be
recorded in shell history or process listings. `--message-stdin` removes that
Sidecar-level exposure, but Cursor CLI still exposes the prompt in its native
child argv. Kimi's ACP prompt does not. Do not send secrets.

With `--json`, stdout is one object with exactly these fields:

- `agent`: selected native agent.
- `session_id`: full selected session ID.
- `outcome`: `completed`, `failed`, `timed_out`, `overflow`,
  `request_pending`, or `audit_error`.
- `delivery`: `delivered` only for eligible Claude/Codex/Cursor native
  success; otherwise `unknown`. Kimi is always `unknown`.
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

- Exit `0`: `outcome: "completed"` and `delivery: "delivered"` for
  Claude/Codex/Cursor.
- Exit `1`: `failed`, `timed_out`, `overflow`, `request_pending`,
  `audit_error`, or a Kimi `completed` result; delivery is unknown.
- Exit `2`: usage, prefix resolution, target eligibility, or other preflight
  rejection before a valid result, including `request_conflict` and
  pre-spawn audit failure.
- Exit `130`: interrupted; delivery is unknown.

Never retry any result with `delivery: "unknown"`; the native agent may already
have received or acted on the message.

### Audit recovery

For a moved namespace with a valid marker, matching key fingerprint, and
strictly valid retained logs, repair the inode binding without losing history:

```text
agent-sidecar audit rebind --allow-write --confirm REBIND-SEND-AUDIT
```

Rebind never reconstructs a missing or corrupt marker. When strict proof is not
available, the supported fallback is an archive reset:

```text
agent-sidecar audit reset --allow-write --confirm CLEAR-SEND-AUDIT
```

Reset takes exclusive nonblocking audit locks and refuses to run while a send
is active. It archives the retained logs, key, and marker under the private
runtime's `audit-archive/` directory, preserving request-ID idempotency history.
It refuses after eight archives rather than silently deleting evidence. Use
the following stronger confirmation only to irreversibly delete active state
and all archives:

```text
agent-sidecar audit reset --purge --allow-write --confirm PURGE-SEND-AUDIT
```

Both operations require an explicit user request; skills and callers must
never invoke them automatically after an audit error. A send failure with
`code: "audit_corrupt"` and `detail: "namespace_moved"` is an informational
hint to use `audit rebind`.

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
printf '%s' '<exact-message>' | agent-sidecar send '<full-session-id>' --agent '<agent-name>' --exact-session --message-stdin --allow-write --request-id '<unique-request-id>' --json
agent-sidecar audit rebind --allow-write --confirm REBIND-SEND-AUDIT
agent-sidecar audit reset --allow-write --confirm CLEAR-SEND-AUDIT
agent-sidecar audit reset --purge --allow-write --confirm PURGE-SEND-AUDIT
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
retry unknown delivery, and never run audit rebind or reset automatically.
