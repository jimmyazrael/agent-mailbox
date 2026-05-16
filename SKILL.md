---
name: agent-mailbox
description: Coordinate autonomous or manual communication with another AI coding agent through shared mailbox files. Use when asked to start, continue, relay, monitor, or manage Codex/Claude Code agent-to-agent collaboration without copy-pasting messages.
---

# Agent Mailbox

Use this skill to coordinate two coding agents through a file-backed mailbox. The v1 implementation is relay-owned: `mailbox_relay.py` invokes each agent for one non-interactive turn, records the response, flips the turn, and repeats until `final`, `blocked`, `error`, or a configured limit.

## Quick Start

Create a mailbox task:

```bash
python scripts/init_mailbox.py --task-id my-task --goal "Review the implementation plan" --project-cwd "F:\Programs\LocalAgentConcept" --first-turn codex
```

Run an autonomous mock relay first:

```bash
python scripts/mailbox_relay.py --task-id my-task --backend mock --mock-responses responses.json
```

Run the real relay only after mock behavior is correct:

```bash
python scripts/mailbox_relay.py --task-id my-task --backend real
```

Inspect state and messages:

```bash
python scripts/read_state.py --task-id my-task --latest 3
python scripts/mailbox_tail.py --task-id my-task --follow
```

## Runtime Layout

Default runtime root:

```text
C:\Users\<user>\.agent-mailbox\
```

Per-task files:

```text
<root>\index.md
<root>\<task-id>\state.json
<root>\<task-id>\messages.md
<root>\<task-id>\relay.log
```

Canonical skill source should live outside any project:

```text
C:\Users\<user>\.agent-skills\agent-mailbox\
```

After review, install copies to both agent skill directories:

```bash
python scripts/install.py
```

## Protocol

- The relay owns `~/.agent-mailbox`. Agents must not modify mailbox files directly.
- Agents end every response with exactly one status marker:
  - `MAILBOX_STATUS: continue`
  - `MAILBOX_STATUS: blocked - <reason>`
  - `MAILBOX_STATUS: final`
- The relay parses only the current agent output. Markers inside peer-message blocks are ignored.
- Peer content is wrapped as untrusted data in `<mailbox-peer-message>` with CDATA-safe escaping.
- The relay enforces `max_rounds`, subprocess timeouts, and known cost/token limits where available.
- Failures append an error message and stop with `state.status = "error"`.

## Scripts

- `scripts/init_mailbox.py`: initialize runtime files for a task.
- `scripts/post_message.py`: append one manual message safely and update state.
- `scripts/read_state.py`: print state and latest messages.
- `scripts/mailbox_relay.py`: run the autonomous relay with `mock` or `real` backend.
- `scripts/mailbox_tail.py`: tail `messages.md`.
- `scripts/mock_agent.py`: deterministic fake agent for relay tests.
- `scripts/install.py`: copy this skill to Claude Code and Codex skill folders.

## Real Backend Requirements

Codex command templates:

```text
codex exec --json --output-last-message <out-file> --full-auto <prompt>
codex exec resume <thread_id> --output-last-message <out-file> --full-auto <prompt>
```

Claude command templates:

```text
claude -p --session-id <uuid> --output-format json --append-system-prompt <text> --permission-mode acceptEdits <prompt>
claude -p -r <uuid> --output-format json --append-system-prompt <text> --permission-mode acceptEdits <prompt>
```

The relay sets subprocess `cwd` to `project_cwd` for both agents. It reads `system-append.md` and passes the text inline to Claude via `--append-system-prompt`.

