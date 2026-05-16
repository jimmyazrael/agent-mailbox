---
name: agent-mailbox
description: Coordinate Codex and Claude Code through a SQLite-backed mailbox with visible WezTerm TUI panes. Use when asked to start, continue, relay, monitor, recover, or manage agent-to-agent collaboration without copy-pasting messages.
---

# Agent Mailbox

Use this skill for two-agent collaboration where Codex and Claude Code should discuss, review, or implement a task while the user keeps native TUI visibility and control.

## Quick Start

From the orchestrator session, start a task:

```powershell
python C:\Users\Jimmy\.agent-skills\agent-mailbox\scripts\mailbox.py start --prefix spc --label "phase 2 review" --goal "Review the design and converge on fixes" --project-cwd F:\Programs\LocalAgentConcept --first-turn codex
```

This opens a WezTerm workspace with Claude, Codex, and relay panes. The bootstrap session returns immediately; the panes are the live interface.

## Inside Agent Panes

When triggered, the pane agent should:

1. Read the latest mailbox state:

```powershell
python <skill>\scripts\mailbox.py show --root <root> --task-id <id> --tail 1 --body --format json
```

2. Decide whether a response is needed. If the previous response already covers the latest message, do not post again.

3. Post via the CLI, never by editing `agent-chat.sqlite` directly:

```powershell
python <skill>\scripts\mailbox.py post --root <root> --task-id <id> --from codex --to claude --status continue --summary "review notes" --body-file F:\tmp\reply.md
```

Valid statuses are `continue`, `blocked`, `final`, and `error`. For `blocked`, also pass `--blocked-reason`.

## Observability And Control

Use native Claude Code and Codex TUIs in WezTerm for progress, thinking, edits, approvals, interrupts, and steering.

Useful control commands:

```powershell
python <skill>\scripts\mailbox.py status --root <root> --task-id <id>
python <skill>\scripts\mailbox.py show --root <root> --task-id <id> --tail 5 --body
python <skill>\scripts\mailbox.py list --root <root> --active-only
python <skill>\scripts\mailbox.py inject --root <root> --task-id <id> --target next --content "New guidance"
python <skill>\scripts\mailbox.py pause --root <root> --task-id <id>
python <skill>\scripts\mailbox.py resume --root <root> --task-id <id>
python <skill>\scripts\mailbox.py stop --root <root> --task-id <id>
python <skill>\scripts\mailbox.py repair --root <root> --task-id <id> --rediscover-codex
```

## Recovery After Crash Or Restart

Use:

```powershell
python <skill>\scripts\mailbox.py resume --root <root> --task-id <id>
```

The mailbox stores Claude's pre-minted session id, Codex's discovered session id, WezTerm pane ids, and the immutable workspace name. Resume rebinds live panes or recreates missing panes when session ids are available. It does not automatically use `codex resume --last`.

## MUST NOT

Agents inside panes must not write directly to `agent-chat.sqlite` or artifact files. Use `mailbox post`.

Agents must not modify other tasks' rooms or messages.

Agents must not call `codex resume --last` automatically.

Agents must not bypass `tui_relay_state.paused` by manually triggering peers.

Markdown transcripts are export-only. Do not parse `transcript.md` back as state.
