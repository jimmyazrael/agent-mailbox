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
