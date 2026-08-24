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
so this residual exposure remains. Never send secrets. Private audit records
avoid storing message and response content, but their key and logs remain
sensitive local state. Delivery reported as unknown must not be retried
automatically because the agent may already have acted.

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
binds a wider address, requires a loopback `Host` authority, requires a
same-origin `Origin` when one is present, refuses declared cross-site
fetches, and forces JSON bodies on state-changing methods. The plugin reaches
the daemon over the Unix socket only; it never reads `http.token` and never
hands any credential to the browser.

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
- review screenshots and terminal output for shell history and adjacent
  sensitive content.

Do not attach raw transcripts, databases or WAL files, `http.token`,
`audit.key`, private keys, credential files, or an unsanitized runtime
directory. `list/status --json` can contain session IDs and local paths, while
`watch --json` can contain transcript-derived event text and remote host
provenance. Sanitize them even when the report itself is private.
