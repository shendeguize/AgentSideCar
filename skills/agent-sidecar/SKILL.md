---
name: agent-sidecar
description: Monitors readonly local AI agent sessions and reports their state and progress. Use when the user asks for agent status, session progress, to monitor agents, or which agent is waiting or working.
---

# Agent Sidecar

Use `agent-sidecar` to inspect local agent sessions without changing their
transcripts, configuration, or hooks.

## Workflow

1. Check `command -v agent-sidecar`.
   - If it is missing, do not improvise another installation. Tell the user to
     run `scripts/install-skill.sh` from the agent_sidecar repository and make
     sure `~/.local/bin` is on `PATH`.
2. Run `agent-sidecar status --json` first. Summarize the active sessions by
   agent, status, title, project, and age from `updated_at`.
3. Use another command only when it matches the request:
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

Never edit agent configuration, install hooks, inject messages, or modify
transcript stores. This skill is observational.

## Interpretation limits

- Cursor IDE can report `waiting` several minutes late because transcript and
  terminal metadata are flushed asynchronously.
- Only local sessions are supported; remote aggregation is deferred.
- Treat `working` and `waiting` as inferred observations, not control-plane
  guarantees. Use `ps --json` as supporting process evidence when needed.

For JSON schemas, status semantics, and the command quick reference, read
[reference.md](reference.md).
