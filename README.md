# Agent Mailbox

Agent Mailbox coordinates Codex and Claude Code through a SQLite-backed mailbox while preserving normal WezTerm TUI visibility. It is intended for two-agent review, planning, implementation, and handoff workflows where the user wants to observe progress and intervene without manually copying messages between agents.

Canonical agent-facing instructions live in [`SKILL.md`](SKILL.md). This README is a human-facing quick guide.

## What It Opens

By default, `mailbox.py start` opens a WezTerm workspace with:

- Claude Code pane
- Codex pane
- relay pane that sends one-shot doorbells to the active agent
- read-only chat transcript pane
- control panel tab for pause/resume/inject/stop/repair actions

The chat transcript and control panel are default-on. Use `--no-chat` or `--no-control-panel` only when you explicitly do not want them.

`start` and `launch-tui` are expected to surface a visible WezTerm GUI window for the task workspace. If a workspace exists only in the mux and no window is visible, that is a launcher bug; run `wezterm start --domain unix --workspace <workspace> --attach` as a manual recovery.

For scripted health checks, use `wezterm cli --prefer-mux list --format json`. Agent Mailbox controls panes through WezTerm's mux; raw `wezterm cli list --format json` can prefer a stale GUI socket on Windows and report a false failure.

## Requirements

Agent Mailbox has a hard runtime dependency on [WezTerm](https://wezterm.org/), because it uses WezTerm panes, tabs, workspaces, and `wezterm cli` to keep Claude Code, Codex, the relay, and the chat transcript visible in one place.

Install WezTerm from the official download page:

```text
https://wezterm.org/installation.html
```

After installation, `wezterm` must be available on `PATH`, or installed in one of WezTerm's standard Windows locations.

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

## Manual Review Mode

Use `review-init` when you want the mailbox/outbox protocol without autonomous panes or a relay:

```powershell
python C:\Users\Jimmy\.agent-skills\agent-mailbox\scripts\mailbox.py review-init `
  --root F:\Programs\LocalAgentConcept\.agent-mailbox `
  --prefix quick-review `
  --goal "Review a small patch with Claude and Codex" `
  --project-cwd F:\Programs\LocalAgentConcept `
  --context-file F:\tmp\quick-review-context.md `
  --format json
```

The command creates the room and outbox folders, then prints:

- `task_id` and `root`
- per-agent prompt text for existing Claude Code / Codex UIs
- per-agent outbox folders
- a `chat_monitor_command` for an optional read-only transcript

No WezTerm panes are spawned in this mode. The user manually tells each agent that the mailbox has a reply, while agents still write normal outbox Markdown files.

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

`<root>` and `<id>` come from the launch output and the control panel:

- `--root <root>` is the mailbox storage directory you passed to `mailbox.py start`, such as `F:\Programs\LocalAgentConcept\.agent-mailbox`.
- `--task-id <id>` is printed by `mailbox.py start` as `task_id` and shown in the control panel as `Task: ...`.
- The control panel also prints `Root: ...`, so if the original terminal output is gone, use the control panel tab to recover both values.
- If you know the root but not the task id, run `python scripts\mailbox.py list --root <root> --active-only`.

Example startup output:

```json
{
  "ok": true,
  "data": {
    "task_id": "spc-dogfood-20260517-1500",
    "root": "F:\\Programs\\LocalAgentConcept\\.agent-mailbox"
  }
}
```

```powershell
python scripts\mailbox.py list --root <root> --active-only
python scripts\mailbox.py status --root <root> --task-id <id>
python scripts\mailbox.py show --root <root> --task-id <id> --tail 5 --body
python scripts\mailbox.py watch-chat --root <root> --task-id <id>
python scripts\mailbox.py control-panel --root <root> --task-id <id>
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

Prefer the default control panel tab for routine accident handling. It exposes:

```text
r refresh | p pause | c resume | i inject | a agent actions/bounce | d rediscover Codex | s stop | q quit panel
```

Use the CLI commands below if the panel is closed or unavailable.

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

Add `--close-panes --yes` only when you also want the WezTerm panes closed. The control panel intentionally does not expose close-panes as a single-key action.

If pane cleanup reports `vanished_after_failure`, the pane kill timed out but a follow-up WezTerm list no longer showed that pane. If the follow-up list itself fails, Agent Mailbox keeps the pane in the failure set and includes `absence_check` so you know the result is uncertain rather than silently treating it as gone.

## Validation

Current scenario coverage includes hard real Claude/Codex/WezTerm cases for context bootstrap, blocked-state injection, duplicate doorbells, Codex rediscovery, context overload, passive consensus, and mid-handoff duplicate-doorbell resilience.

Important work is gated on a real GUI startup smoke. Unit tests and dry-run scenarios are not enough: the smoke must prove that a visible WezTerm workspace exists, Claude and Codex panes are live agent TUIs rather than shells/prompts/auth errors, the relay starts only after readiness, and failed runs clean up panes.

Run unit tests:

```powershell
pytest tests\ -q
```

Run a real scenario only when intentional:

```powershell
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
python scripts\mailbox_eval.py --scenario AM-08 --run-real
```

Run the real GUI startup smoke before dogfooding important work:

```powershell
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
pytest tests\test_real_agents_smoke.py -q -m real_agents
```

Run the prompt-delivery smoke before changing relay send-text behavior:

```powershell
python scripts\prompt_delivery_smoke.py --format json
```

Real scenario runs capture pane snapshots as first-class artifacts under the scenario work root. Scenario contracts should assert observable artifacts such as files, transcript terms, outbox authors/statuses, pause reasons, cleanup dry-run output, or pane snapshots; agent assertions alone are not strong enough for gating important work.

PowerShell note: quote peeled tag refs when verifying annotated tags:

```powershell
git rev-parse --short "v2.0^{}"
```
