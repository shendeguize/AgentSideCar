# Security Policy

## Supported versions

| Version | Security support |
| --- | --- |
| 0.4.x | Supported. Fixes are made against the latest 0.4.x patch, so an upgrade may be required. |
| 0.3.x and older | Unmaintained. Reports are still reviewed on a best-effort basis, but fixes and backports are not promised. |

## Report a vulnerability privately

Do not open a public issue, discussion, or pull request for a suspected
vulnerability, and do not publish exploit details before coordinated
disclosure.

Use GitHub's
[private vulnerability reporting form](https://github.com/shendeguize/AgentSideCar/security/advisories/new).
Include:

- the affected Agent Sidecar version and installation channel;
- the operating system and Python version;
- the security impact and the boundary that is crossed;
- minimal, sanitized reproduction steps or a proof of concept; and
- any mitigations you have already tested.

If GitHub says private reporting is unavailable, do not fall back to a public
issue. The repository owner must enable that GitHub channel before a private
report can be accepted there.

The response targets are an initial acknowledgement within 3 business days,
an initial triage result within 10 business days, and an update at least every
14 calendar days while an accepted report remains active. These are targets,
not service-level guarantees. Remediation and disclosure timing depend on
severity, reproducibility, maintainer availability, and release risk.

## Threat model and security boundaries

Agent Sidecar is a local-first observer of persistence owned by other AI-agent
tools. The local operating-system account, its filesystem permissions, the
agent tools being observed, configured SSH clients, and upstream model
providers are outside Agent Sidecar's trust boundary.

### Local daemon and Unix socket

The default daemon has no TCP listener. It creates a mode-`0700` runtime
directory and mode-`0600` Unix socket and PID file where the platform supports
those permissions. Those controls separate operating-system users; they do not
protect against malicious processes already running as the same user. A custom
runtime directory is an operator-controlled boundary and must not be shared or
placed on an untrusted filesystem.

### Opt-in loopback HTTP and bearer token

HTTP is off by default and must be enabled explicitly. It binds numeric IPv4
`127.0.0.1` only, not `localhost`, IPv6, a wildcard, a LAN interface, or a
remote host. Loopback is not a sandbox: other same-host processes can reach the
port, and forwarding it through SSH, a proxy, a container bridge, or another
tunnel expands the exposure beyond the supported boundary.

Protected HTTP routes require a bearer token. The token is generated when
needed, retained across HTTP restarts in mode-`0600` `http.token` under the
private runtime directory, and is not removed by a normal daemon stop or
service uninstall. The CLI reports the token-file path, never the token. The
browser panel holds it only in page memory. Never put the token in a URL,
cookie, browser storage, command argument, issue, chat, screenshot, or log.
Redact `Authorization` headers completely. If exposure is suspected, stop the
daemon before removing `http.token`; the next explicit HTTP start creates a
replacement.

Strict `Host` and `Origin` checks and the absence of CORS allow headers reduce
browser-origin risk, but do not make a compromised local account trustworthy.
The HTTP adapter is read-only and does not expose send, audit reset, or daemon
control.

### Diagnostics and observed data

The daemon's rotating mode-`0600` JSONL logs use an allowlisted operational
schema intended to omit transcript/message content, responses, filesystem
paths, tokens, cookies, environment values, stdout, and stderr. Short hashes
may still correlate activity within a report. Treat all diagnostics as
sensitive and inspect them before sharing; a defect in redaction is itself a
security issue.

Observation reads metadata, transcript stores, and events owned by supported
agent tools. Their access controls and retention policies remain authoritative.
Agent Sidecar does not encrypt those source files, replace their permissions,
or erase their content.

### SSH remote inventory, transport, and watch

Remote list, status, and watch are explicit opt-in operations. Host selection
comes from DSH Center inventory. Agent Sidecar relies on the user's existing
SSH configuration, pre-trusted host keys, and noninteractive authentication;
it does not enroll host keys, manage credentials, or establish an additional
application-layer encryption boundary.

`--remote-python` and `AGENT_SIDECAR_REMOTE_PYTHON` are operator-controlled
inputs at the local-to-remote execution boundary. The value must be a nonempty
absolute path of at most 1024 characters, contain only
`[A-Za-z0-9._+/-]`, and have no `..` path segment; invalid values are rejected
locally before any SSH connection. A valid value is passed through
`shlex.join` as one argv token to the fixed probe script and bootstrap command,
never interpolated into script text. The probe uses only that one candidate.
If it is missing, non-executable, or older than Python 3.8 on a host, that host
fails closed with `python_too_old`; Agent Sidecar does not fall back to another
candidate or bare `python3`. The bootstrap uses the same validated operator
token verbatim. Probe response fields remain fully validated, but a differing
returned executable cannot replace an explicit pin.

For bounded default discovery, the remote-produced interpreter executable path
is validated locally before reuse: it must be a nonempty absolute path of at
most 1024 characters, contain only `[A-Za-z0-9._+/-]`, and have no `..` path
segment. It is then inserted as a separate token through `shlex.join`. Probe
and bootstrap use separate SSH connections, so replacement at the selected
bootstrap path remains possible between them, equivalent to the prior
bare-Python re-resolution window. This is session-local path reuse, not inode
or file-descriptor binding.

The active Agent Sidecar package is streamed as a bounded temporary zipapp,
executed with remote Python, and removed during cleanup. Remote data is then
rendered locally. Inventory entries, host aliases, addresses, authentication
failures, remote paths, and emitted events can all be sensitive. Remote watch
has no automatic reconnect, can have observation gaps after failures, and must
not be treated as an audit-complete event stream. Remote monitoring does not
support message delivery.

### Experimental message injection and `send`

The experimental message-injection path is exposed as `agent-sidecar send` and
is separate from observation. It requires an explicit request and
`--allow-write`, starts a native headless-resume process, and may cause that
agent to contact its provider, run tools, modify its session, or change
workspace files. It is not a sandbox or a policy-enforcement boundary.

A positional message is present in the Sidecar command line, so it may appear
in shell history or process listings; `send --message-stdin` reads the message
from standard input and keeps it out of the Sidecar command line. For Cursor
CLI the prompt is still passed on the native child command line whichever
source is used, because that upstream resume contract requires argv transport,
so this residual exposure remains. Kimi's protected ACP path carries the
message only in the `session/prompt` NDJSON frame and does not place it in
either Sidecar's or native Kimi's argv. Never send secrets. Private audit
records avoid storing message and response content, but their key and logs
remain sensitive local state. Delivery reported as unknown must not be retried
automatically because the agent may already have acted.

The send-audit namespace is persistently bound to its canonical runtime inode,
home-anchor marker, key identity, and retained history. Do not delete, move,
recreate, or replace `AGENT_SIDECAR_RUNTIME_DIR`, the marker, or audit files to
clear `unsafe_lock`: that result means the previous namespace history or
identity cannot be proved. Preserve it and fail closed; deleting evidence does
not authorize a retry. Only an explicitly new request lineage may select a new
owner-private runtime path and new request ID, and that new namespace and its
audit history must then be retained. It is not recovery of the old lineage.

Kimi mutation is limited to the exact supported Kimi Code `0.38.0` and a
top-level local session observed as `waiting` or `idle`. Sidecar binds and
revalidates the selected Kimi home, project sources, session index/state
identity, main-agent home, root session directory, and root wire. The send
plan additionally binds a private snapshot of the package assets and, for the
Node distribution, the Node executable plus non-system dylib closure. Version
and ACP initialization are reprobed from the snapshot, and an owner-process
guard rejects another Kimi or matching Node owner in the project. That bounded
guard recognizes direct Kimi hints and executable identity and parses only
canonical Node file arguments, including supported preload/import/loader and
later script forms. It does not turn arbitrary long ordinary Node commands
into blockers. The required owner-safe executable is single-link; with that
boundary, an ordinary relative Node command with an inaccessible/deleted cwd
is ignored. Kimi hints, identity matches, malformed or over-budget input that
carries such evidence, and unsafe or changed executable identity still fail
closed.

Those mechanisms protect the local spawn-resume boundary against the specific
filesystem, replacement, ownership, and concurrent-owner races they check.
They do not cryptographically bind a request ID or audit receipt to one remote
provider turn, and they do not turn Kimi into a sandbox. The ACP client
advertises no MCP, filesystem, or terminal capability; it resumes with no MCP
servers. Permission reverse requests receive `cancelled`; any question or
approval path is cancelled by failing closed, and unsupported reverse methods
end the run. The resumed Kimi process and provider still retain their own
security properties.

Kimi has no live inbox or steering path. `working`, child, remote, dead, and
malformed Kimi targets fail closed. Darwin fork containment may retain a
`cleanup_incomplete` diagnostic. Only when ACP returns rc `0`, settles at
`end_turn`, and strict durable root-wire and state proof identifies the
matching completed turn does the public result become `outcome: "completed"`,
`delivery: "unknown"`, with CLI exit `1`; it is never delivered. Without that
proof it remains failed (or the bounded timeout/overflow outcome) and unknown.
Do not retry the same content under a new request ID. Reusing the same retained
request ID can only replay its audit receipt and does not spawn again. Claude,
Codex, and Cursor retain their existing send semantics.

`send --agent NAME` optionally restricts selection to an exact,
case-insensitive agent name, and `--exact-session` optionally disables prefix
matching. The DSH plugin always combines them for external delivery, so a
known agent and full session ID form one exact target. Both `send` and the
plugin validate the returned agent, full session ID, and request ID against
the selected request; an identity mismatch is delivery-unknown
`executor_error`, never success and never an automatic retry.

Agent Sidecar makes no general claim of sandboxing, process isolation, data-at-
rest encryption, or end-to-end encryption. Platform permissions, SSH, and
provider transports retain their own independent security properties. See the
[product reference](skills/agent-sidecar/reference.md) for the exact command,
protocol, and compatibility boundaries.

### DSH plugin routes and trust posture

The optional DSH plugin (`plugin/`, published as
`@shendeguize/dsh-agent-sidecar`) registers its own routes on the dsh web
server under `/plugins/agent-sidecar/api`. The dsh web server has no
authentication layer and its `/api` fence does not cover plugin routes, so
the plugin carries its own guard: it refuses non-loopback peers even when dsh
binds a wider address and requires exactly one loopback `Host` authority. A
single valid loopback Host remains accepted on any valid port. On the current
cleartext HTTP carrier, an `Origin`, when present, must also appear exactly
once and use `http` with the same normalized host and effective port; HTTPS
origins are rejected. Duplicate `Host` or `Origin` fields fail closed even
when their values match. The guard also refuses declared cross-site fetches
and forces JSON bodies on state-changing methods. The plugin reaches the
daemon over the Unix socket only; it never reads `http.token` and never hands
any credential to the browser.

Those checks defend against browser-mediated attacks such as CSRF and DNS
rebinding. They do not authenticate the caller: any process on the same
machine that can open a loopback TCP connection can read the plugin's
normalized session-event data and, when injection is enabled, complete the
write flow. That is the same trust posture as dsh's own unauthenticated
`/api` surface, and it is deliberately weaker than the sidecar's own
boundaries, where the Unix socket relies on same-user mode-`0600` file
permissions and the optional HTTP read surface requires a bearer token. Treat
this as an explicitly declared trade-off, not silent inheritance.

Injection through the plugin is off by default (`inject.enabled: false`), and
every injection requires a server-issued, one-time, short-lived confirm token
in a two-phase prepare/execute flow. That confirmation is verifiable against
browser-mediated requests, but it cannot bind a local process to user
consent, so on the plugin channel the explicit same-turn user-request rule of
`send` degrades to a UX convention rather than a technical guarantee. Do not
enable `inject.enabled` on a multi-user host, and be aware that normalized
session-event content is readable by same-host processes while the plugin is
mounted.

The global switch does not replace per-session eligibility. External
injection is limited to local top-level Claude, Codex, Cursor CLI, and Kimi
sessions observed as `waiting` or `idle`; external working, child/sidechain,
remote, dead, malformed, and unsupported targets fail closed. Kimi uses its
protected manual spawn-resume path and never gains live queue/steer semantics.
Local DSH working sessions can use the in-process steer path, while persisted
DSH waiting, idle, and cold/no-status sessions enter live/cold preflight.
Disabled controls expose a localized accessible reason instead of silently
disappearing.

For cold DSH targets, the board is only a staleable observation. If
authoritative persistence proves the target missing, prepare returns HTTP 404
`target_not_found`; a proved effective preset is unsupported and returns HTTP
409 `dsh_preset_unsupported`; an absent, failed, reloaded, malformed, or
otherwise indeterminate persistence/preset authority returns HTTP 502
`executor_error`. None of these rejections issues a confirmation token or
starts a resume.

Cold DSH resume requires the host to resolve a complete current default
provider/model pair; missing or partial model configuration returns HTTP 409.
For a live DSH target, queue/steer acceptance means only that the synchronous
inbox accepted or queued the message. It is not proof of model execution or
success, so the operator must observe the target timeline. Direct CLI send
does not support DSH.

Nested modal isolation mutates only attributes it owns. HMR handoff drains
queued lifecycle records, and remove/reinsert races retain the current opener
instead of restoring stale focus. Content-free real acceptance records only
Kimi protected-resume PASS and DSH steer/UI PASS; private paths, identifiers,
content, and hashes are not acceptance evidence.

The local owner-facing UI may display full project paths to distinguish local
workspaces. Timeline `sourceOutcomes`, `degraded`, and `reason` are deliberately
content-free, but timeline entries, project paths, session identifiers, and
screenshots remain sensitive. Local visibility is not authorization to publish
that evidence.

AI bypass analysis is likewise off by default (`analysis.enabled: false`)
because it consumes model tokens. Analysis runs in dedicated, individually
stoppable dsh sessions with bounded input truncation, bounded turn timeouts,
and a bounded number of concurrent sessions; there is no automatic or
periodic analysis. Analyzed session content, follow-up questions, and model
replies are never written to the plugin's logs.

## Sharing diagnostics safely

Prefer a minimal reproduction with synthetic data. Before attaching any
`--json` output or diagnostic excerpt:

- replace usernames, home directories, project and transcript paths, session
  and request identifiers, and other stable identifiers with placeholders;
- remove prompts, responses, transcript/event text, command arguments, tokens,
  authorization headers, cookies, environment values, and provider data;
- remove SSH host aliases, hostnames, IP addresses, inventory details, remote
  paths, usernames, and authentication material; and
- review screenshots and terminal output for plugin-displayed project paths,
  shell history, and adjacent sensitive content.

Do not attach raw transcripts, databases or WAL files, `http.token`,
`audit.key`, private keys, credential files, or an unsanitized runtime
directory. `list/status --json` can contain session IDs and local paths, while
`watch --json` can contain transcript-derived event text and remote host
provenance. Sanitize them even when the report itself is private.
