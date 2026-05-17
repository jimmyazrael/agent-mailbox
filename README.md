# Agent Mailbox

Agent Mailbox coordinates Codex and Claude Code through a SQLite-backed mailbox while preserving normal WezTerm TUI visibility. It is intended for two-agent review, planning, implementation, and handoff workflows where the user wants to observe progress and intervene without manually copying messages between agents.

Canonical agent-facing instructions live in [`SKILL.md`](SKILL.md). This README is a human-facing quick guide.

## What It Opens

By default, `mailbox.py start` opens a WezTerm workspace with:

- Claude Code pane
- Codex pane
- relay pane that triggers the active agent
- read-only chat transcript pane

The chat transcript is default-on. Use `--no-chat` only when you explicitly do not want it.

## Day-To-Day Invocation

From Codex or Claude Code, you should be able to ask naturally:

```text
Start agent-mailbox for reviewing this plan with Claude.
Use agent-mailbox to have Claude and Codex collaborate on this feature.
Start communication with Claude Code about the SPC pipeline implementation.
```

The orchestrator agent should inspect obvious project context, ask short clarification questions if required, write a bootstrap context file, and launch the mailbox.

## Direct CLI Start

```powershell
python C:\Users\Jimmy\.agent-skills\agent-mailbox\scripts\mailbox.py start `
  --root F:\Programs\LocalAgentConcept\.agent-mailbox `
  --prefix spc-dogfood `
  --label "SPC injection pipeline implementation" `
  --goal "Dogfood agent-mailbox on the SPC injection pipeline implementation" `
  --project-cwd F:\Programs\LocalAgentConcept `
  --first-turn claude `
  --context-file F:\tmp\spc-dogfood-context.md
```

Use `claude` first for design review, ambiguity reduction, or second-opinion planning. Use `codex` first when the implementation task is already clear.

## Bootstrap Context

For serious tasks, the bootstrap context should include:

- goal and expected output
- project root and git status summary
- relevant files and why they matter
- constraints and files/commands/environments not to touch
- whether edits are allowed
- suggested workflow and first owner
- done criteria and verification expectations

If any of these are unclear and risky to assume, the orchestrator should ask the user 1-3 concise clarification questions before launch.

## Useful Commands

```powershell
python scripts\mailbox.py status --root <root> --task-id <id>
python scripts\mailbox.py show --root <root> --task-id <id> --tail 5 --body
python scripts\mailbox.py watch-chat --root <root> --task-id <id>
python scripts\mailbox.py inject --root <root> --task-id <id> --target next --content "New guidance"
python scripts\mailbox.py pause --root <root> --task-id <id>
python scripts\mailbox.py resume --root <root> --task-id <id>
python scripts\mailbox.py stop --root <root> --task-id <id>
python scripts\mailbox.py repair --root <root> --task-id <id> --rediscover-codex
```

## Recovery

Use `resume` after a crash or restart:

```powershell
python scripts\mailbox.py resume --root <root> --task-id <id>
```

The mailbox stores Claude session metadata, discovered Codex session metadata, WezTerm pane ids, and the immutable workspace name. It does not automatically use `codex resume --last`; that fallback remains manual.

## Accident Playbook

Start with status:

```powershell
python scripts\mailbox.py status --root <root> --task-id <id>
```

If an agent looks stuck:

```powershell
python scripts\mailbox.py inject --root <root> --task-id <id> --target next --content "You appear stuck. Summarize current state, say what you are waiting on, and either continue or post blocked."
```

If you need to stop the automation but keep panes open:

```powershell
python scripts\mailbox.py pause --root <root> --task-id <id> --reason "user inspection"
```

Resume after inspection:

```powershell
python scripts\mailbox.py resume --root <root> --task-id <id>
```

If one TUI pane crashed or was closed:

```powershell
python scripts\mailbox.py resume --root <root> --task-id <id>
```

If Codex cannot be resumed because its session id is missing, try rediscovery after Codex has posted at least once:

```powershell
python scripts\mailbox.py repair --root <root> --task-id <id> --rediscover-codex
python scripts\mailbox.py resume --root <root> --task-id <id>
```

If an agent hits a 403, auth failure, rate limit, or approval wall:

```powershell
python scripts\mailbox.py pause --root <root> --task-id <id> --reason "auth or approval issue"
```

Then fix the issue directly in the affected TUI pane or outside the mailbox. After it is resolved:

```powershell
python scripts\mailbox.py inject --root <root> --task-id <id> --target next --content "Auth/approval issue is resolved. Continue from the latest mailbox state."
python scripts\mailbox.py resume --root <root> --task-id <id>
```

If the agents are going in the wrong direction:

```powershell
python scripts\mailbox.py inject --root <root> --task-id <id> --target next --content "Correction: <new guidance>. Stop the previous direction and restate the new plan before continuing."
```

If you need a hard stop:

```powershell
python scripts\mailbox.py stop --root <root> --task-id <id>
```

Add `--close-panes --yes` only when you also want the WezTerm panes closed.

## Validation

Tagged releases:

- `v1.0`: initial validated two-agent workflow, AM-01 through AM-06
- `v1.1`: default-on chat transcript, AM-07 duplicate-handoff scenario, protocol vocabulary expansion

Current scenario coverage includes hard real Claude/Codex/WezTerm cases for context bootstrap, blocked-state injection, duplicate triggers, Codex rediscovery, context overload, passive consensus, and mid-handoff duplicate-trigger resilience.

Run unit tests:

```powershell
pytest tests\ -q
```

Run a real scenario only when intentional:

```powershell
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
python scripts\mailbox_eval.py --scenario AM-01 --run-real --keep
```
