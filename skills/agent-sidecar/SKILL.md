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
   - If it is missing, do not improvise another installation. Tell the user to
     run `scripts/install-skill.sh` from the agent_sidecar repository and make
     sure `~/.local/bin` is on `PATH`.
2. Run `agent-sidecar status --json` first. Summarize the active sessions by
   agent, status, title, project, and age from `updated_at`.
3. Use another observation command only when it matches the request:
   - `agent-sidecar list --json` lists sessions updated in the last 48 hours.
   - `agent-sidecar list --all --json` includes older sessions.
   - `agent-sidecar ps --json` checks supported local agent processes.
   - `agent-sidecar watch <session-prefix> --json` follows one session.
   - `agent-sidecar watch --all --json` follows normalized events from all
     sessions.
   - `agent-sidecar tui` opens the interactive terminal dashboard; `q` exits.
4. Start the daemon with `agent-sidecar daemon start` only when the user asks
   for continuous monitoring, the TUI, or explicitly asks to start it. The
   command is idempotent. Status and list queries work without it by falling
   back to direct readonly scans.
5. Stop the daemon with `agent-sidecar daemon stop` only when the user asks.

For remote monitoring, run `list --remote --json` or `status --remote --json`
only when the user asks for remote or cross-host results. Add repeatable
`--host <host-alias>` filters only when the user identifies those hosts.
Remote rows include a `host` provenance field; local rows are marked `local`.
Do not use remote mode by default.

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
run exactly one command:

```sh
agent-sidecar send <session-prefix> "<exact-message>" --allow-write --json
```

The only eligible targets are local, top-level `claude`, `codex`, and
`cursor-cli` sessions in `waiting` or `idle`. Never send to a remote,
`working`, `dead`, child, or sidechain session. Never send to `cursor-ide`,
`copilot`, `kimi`, `dsh`, or another unsupported agent. `send` performs its own
direct local scan; it does not use the daemon or remote aggregation.

The command starts another blocking native process. It does not interrupt a
live process and does not fork the original session. The resumed agent may
contact its provider, run tools, and modify agent-owned state or workspace
files.

Never automatically retry `failed`, `timed_out`, or `overflow`, an interrupted
command, or any result with `delivery: "unknown"`. The agent may already have
received or acted on the message. Report the result and ask the user what to do
next.

The message is a positional argument in the sidecar command, so it can appear
in shell history and process listings. Claude and Codex pass the native prompt
through stdin, but Cursor CLI necessarily includes it in the child argv too.
Warn against sending secrets through this command.

Outside an explicit send request, never edit agent configuration, install
hooks, inject messages, or modify transcript stores.

## Interpretation limits

- Cursor IDE can report `waiting` several minutes late because transcript and
  terminal metadata are flushed asynchronously.
- Remote support is snapshot-only for `list` and `status`; remote watch and
  remote send are unavailable.
- Treat `working` and `waiting` as inferred observations, not control-plane
  guarantees. Use `ps --json` as supporting process evidence when needed.

For JSON schemas, status semantics, and the command quick reference, read
[reference.md](reference.md).
