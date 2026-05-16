---
name: agent-mailbox
description: Coordinate Codex and Claude Code through a SQLite-backed mailbox with visible WezTerm TUI panes. Use when asked to start, continue, relay, monitor, recover, or manage agent-to-agent collaboration without copy-pasting messages.
---

# Agent Mailbox

Use this skill for two-agent collaboration where Codex and Claude Code should discuss, review, or implement a task while the user keeps native TUI visibility and control.

## Quick Start

From the orchestrator session, start a task:

```powershell
python C:\Users\Jimmy\.agent-skills\agent-mailbox\scripts\mailbox.py start --prefix spc --label "phase 2 review" --goal "Review the design and converge on fixes" --project-cwd F:\Programs\LocalAgentConcept --first-turn codex --context-file F:\tmp\spc-context.md
```

This opens a WezTerm workspace with Claude, Codex, and relay panes. The bootstrap session returns immediately; the panes are the live interface.

Use `--context-file` or `--context` for serious tasks. The context should state scope, relevant files, constraints, done criteria, and what must not be touched. It is stored as a bootstrap mailbox message so fresh TUI sessions do not have to infer project background from scratch.

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

## Role And Mode Protocol

Agents are equal peers for design and review, but every non-terminal round must have one concrete owner. This prevents passive consensus where both agents agree and nobody acts.

Every non-terminal mailbox post should begin with:

```text
Mode: DISCUSS | EXECUTE | REVIEW | BLOCKED | DONE
Coordinator: <agent>
Owner: <agent-or-none>
Reviewer: <agent-or-none>
Next action: <one concrete action, or "none">
Done when: <observable completion condition>
Blocked on: <only when blocked>
```

Mode meanings:

`DISCUSS`: resolve uncertainty, design choices, or risks.

`EXECUTE`: one owner performs a concrete action.

`REVIEW`: peer checks concrete output.

`BLOCKED`: human approval, credentials, destructive action, unclear requirement, or external dependency.

`DONE`: terminal consensus for the task.

Default coordinator is the agent that initialized the task or made the latest material change. The coordinator is not a boss; it prevents drift by naming mode, owner, and next action. The reviewer may object with `Mode objection: ...`.

Rule: a non-terminal message must not end with agreement only. It must either name a concrete `Next action` with an owner and done condition, or declare `Blocked on`.

`mailbox.py post` emits non-blocking warnings when non-terminal posts omit the protocol header. Warnings are advisory for compatibility, but new skill usage should treat them as issues to fix.

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

The repair command intentionally does not support `--use-last-codex-session` even with `--yes`; if you must fall back to the most recent Codex session, run `codex resume --last` manually inside a pane.

## MUST NOT

Agents inside panes must not write directly to `agent-chat.sqlite` or artifact files. Use `mailbox post`.

Agents must not modify other tasks' rooms or messages.

Agents must not call `codex resume --last` automatically.

Agents must not bypass `tui_relay_state.paused` by manually triggering peers.

Markdown transcripts are export-only. Do not parse `transcript.md` back as state.

## Behavioral Evaluation

Before using this skill on important work, prefer hard real-agent scenarios over mocks:

```powershell
python <skill>\scripts\mailbox_eval.py --scenario AM-01
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
python <skill>\scripts\mailbox_eval.py --scenario AM-01 --run-real --keep
```

Scenario definitions live under `eval/scenarios`. They should be adversarial and designed to expose failures: context loss, approval friction, blocked-state handling, duplicate triggers, rediscovery, and context overload.
