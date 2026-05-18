---
name: agent-mailbox
description: Coordinate Codex and Claude Code through a SQLite-backed mailbox with visible WezTerm TUI panes. Use when asked to start, continue, relay, monitor, recover, or manage agent-to-agent collaboration without copy-pasting messages.
---

# Agent Mailbox

Use this skill for two-agent collaboration where Codex and Claude Code should discuss, review, or implement a task while the user keeps native TUI visibility and control.

## Day-To-Day Invocation

The user should not need to remember mailbox flags or a context checklist. When the user says something like:

```text
Start agent-mailbox for <task>.
Start communication with Claude Code about <task>.
Use two agents to review/implement <task>.
```

the orchestrator agent should:

1. Infer the project root from the current workspace unless the user names another path.
2. Inspect obvious local context first: repo instructions (`CLAUDE.md`, `AGENTS.md`, `README.md`), relevant plans/specs/docs named by the user, and `git status`.
3. Ask the user concise clarification questions before launching if required information is missing or risky to assume.
4. Write a concise bootstrap context file under the temp directory, e.g. `F:\tmp\agent-mailbox-context-<slug>.md`.
5. Start `mailbox.py start` with that context file. Do not ask the user to manually craft the context unless required information is missing.
6. Tell the user the task id, mailbox root, and that WezTerm now contains Claude, Codex, relay, and chat transcript views.

Clarification gate:

Ask before launching when any of these are unclear:

- The concrete goal or expected output.
- The project root or target files.
- Whether agents should review, implement, or both.
- Whether edits are allowed.
- Files, directories, commands, or environments that must not be touched.
- Destructive, expensive, remote-only, credentialed, or approval-requiring operations.
- The done criteria or verification expectations.

Keep questions short and grouped. Prefer 1-3 questions. Do not ask if a reasonable safe default exists and the task can proceed read-only.

Bootstrap context file template:

```text
# Agent Mailbox Bootstrap Context

Goal:
- <one concrete outcome>

Project:
- root: <absolute project path>
- current branch/status summary: <short git status summary>

Relevant Files:
- <path>: <why it matters>

Constraints:
- <must follow>
- <must not touch>
- <verification limits, remote-only caveats, approvals needed>

Suggested Workflow:
- First turn: <claude|codex> should <review/design/implement>
- Peer should <review/implement/verify>
- Use the role/mode protocol and assign one owner per non-terminal round.

Done Criteria:
- <observable completion condition>
- <tests/scenarios/commands expected, or explicitly note if unavailable>
```

Default first turn:

- Use `claude` for broad design review, ambiguity reduction, or second-opinion planning.
- Use `codex` for direct local implementation when the required change is already clear.

From the orchestrator session, start a task:

```powershell
python C:\Users\Jimmy\.agent-skills\agent-mailbox\scripts\mailbox.py start --prefix spc --label "phase 2 review" --goal "Review the design and converge on fixes" --project-cwd F:\Programs\LocalAgentConcept --first-turn codex --context-file F:\tmp\spc-context.md
```

This opens a WezTerm workspace with Claude, Codex, relay, and read-only chat transcript views. The bootstrap session returns immediately; the panes are the live interface.

The WezTerm GUI must be visibly surfaced after launch. If `mailbox.py start` returns pane ids but no window appears, treat that as a launcher failure and attach manually with `wezterm start --domain unix --workspace <workspace> --attach`, then fix the launcher before relying on the task.

For health checks and scripted control, use the same mux-targeted path as the launcher:

```powershell
wezterm cli --prefer-mux list --format json
```

Do not use raw `wezterm cli list --format json` as a gate. On Windows it can prefer a stale GUI socket even when the mux used by Agent Mailbox is healthy.

Use `--no-chat` only when you explicitly do not want the transcript monitor:

```powershell
python C:\Users\Jimmy\.agent-skills\agent-mailbox\scripts\mailbox.py start --prefix spc --label "phase 2 review" --goal "Review the design and converge on fixes" --project-cwd F:\Programs\LocalAgentConcept --first-turn codex --context-file F:\tmp\spc-context.md --no-chat
```

Use `--context-file` or `--context` for serious tasks. The context should state scope, relevant files, constraints, done criteria, and what must not be touched. It is stored as a bootstrap mailbox message so fresh TUI sessions do not have to infer project background from scratch.

## Inside Agent Panes

When the doorbell arrives in a pane, the pane agent should:

1. Read the latest mailbox state:

```powershell
python <skill>\scripts\mailbox.py show --root <root> --task-id <id> --tail 1 --body --format json
```

2. Decide whether a response is needed. If the previous response already covers the latest message, do not post again.

3. Reply by writing the assigned outbox Markdown file. Do not call `mailbox.py post` for normal agent-to-agent turns.

```markdown
---
from: codex
to: claude
status: continue
summary: review notes
---

Mode: REVIEW
Coordinator: codex
Owner: claude
Reviewer: codex
Next action: inspect the findings and decide whether to patch
Done when: Claude either accepts the patch or identifies a concrete issue

Review body...

<!-- AGENT-MAILBOX:DONE -->
```

Valid statuses are `continue`, `blocked`, `final`, and `error`. For `blocked`, include `Blocked on:` in the body. The relay imports completed outbox files and mirrors them to SQLite.

### Mentioning The Sentinel In Body Prose

The parser only treats the completion sentinel as active when it appears on its own line with no other content before or after it on that line. To mention the sentinel literally in prose, wrap it in inline code or a fenced code block.

Valid literal mention:

```markdown
Mention `<!-- AGENT-MAILBOX:DONE -->` when explaining the format.
```

Invalid bare sentinel line inside body prose:

```markdown
Do not place this line before the true end of the message:
<!-- AGENT-MAILBOX:DONE -->
More body text here.
```

## Role And Mode Protocol

Agents are equal peers for design and review, but every non-terminal round must have one concrete owner. This prevents passive consensus where both agents agree and nobody acts.

Every non-terminal mailbox post should begin with:

```text
Mode: DISCUSS | EXECUTE | REVIEW | BLOCKED | DONE | INVESTIGATE | RESEARCH | PLAN | COORDINATE | INFORM
Coordinator: <agent>
Owner: <agent-or-none>
Reviewer: <agent-or-none>
Next action: <one concrete action, or "none">
Done when: <observable completion condition>
Blocked on: <only when blocked>
```

Mode meanings:

`DISCUSS`: resolve uncertainty, design choices, or risks.

`INVESTIGATE` / `RESEARCH` / `PLAN`: gather context or propose an approach before execution.

`EXECUTE`: one owner performs a concrete action.

`REVIEW`: peer checks concrete output.

`COORDINATE`: assign ownership, sequence handoffs, or resolve who does what next.

`INFORM`: provide status or evidence without changing ownership.

`BLOCKED`: human approval, credentials, destructive action, unclear requirement, or external dependency.

`DONE`: terminal consensus for the task.

Default coordinator is the agent that initialized the task or made the latest material change. The coordinator is not a boss; it prevents drift by naming mode, owner, and next action. The reviewer may object with `Mode objection: ...`.

Rule: a non-terminal message must not end with agreement only. It must either name a concrete `Next action` with an owner and done condition, or declare `Blocked on`.

The outbox importer rejects malformed files. Treat a malformed-outbox pause as a real protocol failure and fix the outbox file rather than bypassing the importer.

If two possible numbering systems collide in a discussion, prefer the mailbox message id plus the scenario/task id over conversational round numbers. For example, say `AM-07 message 4` rather than `the second 4th item`.

### Authorization State Protocol

For agent-mailbox development or any multi-item agent-to-agent task, use this state model. Do not start implementation from critique alone.

```text
DISCUSS    - open critique. Either party raises issues. No commits.
PROPOSE    - one party drafts a named subset. Other party reviews. No commits.
AUTHORIZE  - user explicitly names the subset to implement.
EXECUTE    - owner makes commits. Use one commit per authorized item unless the user says otherwise.
REVIEW     - reviewer checks the commit against the acceptance checklist before the next item starts.
DONE       - terminal. New work requires fresh AUTHORIZE.
```

Valid authorization examples:

```text
Authorized: A, B, E from the proposal.
I pick Option 2.
Implement C only.
```

Ambiguous phrases like "sounds good", "continue", or "standing down" are not implementation authorization. Treat `DONE` or "standing down" as terminal, not as permission to work parked backlog.

First-pass acceptance checklist for reviewers:

- Diff matches the authorized scope; no drive-by changes.
- Required tests pass, or missing tests are explicitly justified.
- Documentation matches behavior.
- Names are honest: a scenario or command name describes what it actually exercises.
- No silent failure paths were introduced.
- For hard scenarios, the contract is artifact-anchored and exercises what the scenario name implies.

## Migrating From v1

Current mailbox databases use schema version 2. Opening a database with an absent, older, or newer schema version fails with `schema_version_mismatch` instead of auto-migrating.

For a v1 database, write a fresh v2 database with:

```powershell
python <skill>\scripts\migrate_v1_to_v2.py <old-agent-chat.sqlite> <new-agent-chat.sqlite>
```

The migration copies rooms, messages, receipts, room state, pane/session metadata, and rebuilds `message_sources` for legacy outbox messages. Do not point the destination at the source file.

## Observability And Control

Use native Claude Code and Codex TUIs in WezTerm for progress, thinking, edits, approvals, interrupts, and steering.

Mailbox launches a read-only chat transcript tab and a control panel tab by default. Keep the control panel available unless the user explicitly asks for `--no-control-panel`; it is the normal place for pause, resume, inject, stop, rediscover, and bounce-agent actions.

`codex_discovery_status=failed` means the scanner did not find a Codex session id yet. In non-rediscovery scenarios this can be informational noise, not proof Codex is broken. Treat it as actionable only when resume/repair needs a Codex session id or a scenario explicitly requires `discovered`.

Useful control commands:

```powershell
python <skill>\scripts\mailbox.py status --root <root> --task-id <id>
python <skill>\scripts\mailbox.py show --root <root> --task-id <id> --tail 5 --body
python <skill>\scripts\mailbox.py watch-chat --root <root> --task-id <id>
python <skill>\scripts\mailbox.py control-panel --root <root> --task-id <id>
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

If `stop --close-panes` reports `vanished_after_failure`, a timed-out pane kill was followed by a successful pane list where the pane was absent, so the pane is classified as already gone. If the follow-up list fails, the cleanup is intentionally biased safe: the pane remains a failure with `absence_check` explaining the uncertainty.

### Recovery From A Degraded WezTerm Mux

Symptom: `wezterm cli --prefer-mux list --format json` times out or hangs.

1. Run `python <skill>\scripts\mailbox.py doctor-wezterm --format json` to inspect WezTerm processes, mux responsiveness, stuck CLI helpers, and Agent Mailbox-shaped workspaces.
2. Preview task-scoped cleanup with `python <skill>\scripts\mailbox.py reset-wezterm --task-scoped --dry-run --format json`.
3. If the plan only contains Agent Mailbox or stuck mux-list helper processes, run `python <skill>\scripts\mailbox.py reset-wezterm --task-scoped --yes --format json`.
4. Re-run `doctor-wezterm` and the mux health command.
5. If still degraded, preview global cleanup with `reset-wezterm --global --dry-run`. Do not execute global cleanup without explicit user approval; execution requires `--yes-global` and kills the WezTerm mux/client processes.
6. Do not chain repeated `wezterm start --always-new-process` restarts. If task-scoped cleanup and one fresh start do not recover the mux, stop and investigate.

## Accident Handling

When the user reports that an agent crashed, got stuck, hit 403/auth/rate-limit errors, needs approval, or is going in the wrong direction:

- Prefer the control panel tab for `p` pause, `i` inject, `c` resume, `a` agent actions/bounce, `d` rediscover Codex, and `s` stop.
- If the panel is unavailable, start with `mailbox.py status --root <root> --task-id <id>`.
- Use `pause` before manual inspection or credential/approval fixes.
- Use `inject` to provide corrective guidance or tell the active agent to summarize what it is waiting on.
- Use `resume` after panes, credentials, or approvals are fixed.
- Use `repair --rediscover-codex` if Codex session metadata is missing after Codex has posted at least once.
- Use `stop` for a hard stop; add `--close-panes --yes` only when the user explicitly wants panes closed.

For detailed user-facing commands, see `README.md` "Accident Playbook".

## MUST NOT

Agents inside panes must not write directly to `agent-chat.sqlite` or artifact files. Use the assigned outbox Markdown file.

Agents must not modify other tasks' rooms or messages.

Agents must not call `codex resume --last` automatically.

Agents must not bypass `tui_relay_state.paused` by manually sending doorbells to peers.

Markdown transcripts are export-only. Do not parse `transcript.md` back as state.

## Behavioral Evaluation

Before using this skill on important work, prefer hard real-agent scenarios over mocks:

```powershell
python <skill>\scripts\mailbox_eval.py --scenario AM-01
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
python <skill>\scripts\mailbox_eval.py --scenario AM-08 --run-real
```

Before dogfooding important work, run a real GUI startup smoke. It must prove that a visible WezTerm workspace exists, Claude and Codex panes are live agent TUIs rather than shells/prompts/auth errors, relay starts only after readiness, and failed runs clean up panes:

```powershell
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
pytest <skill>\tests\test_real_agents_smoke.py -q -m real_agents
```

Scenario definitions live under `eval/scenarios`. They should be adversarial and designed to expose failures: context loss, approval friction, blocked-state handling, duplicate doorbells, rediscovery, and context overload.

Every scenario declares a `tier`:

- `real`: intended for real Claude/Codex/WezTerm execution; a non-real run only proves initialization.
- `synthetic`: exercises mailbox runtime paths with controlled fakes or monkey-patched adapters.
- `unit`: reserved for deterministic checks that should usually live under `tests/` instead of `eval/scenarios`.

A synthetic pass must not be described as proof of real-agent behavior. If a scenario uses `synthetic_action`, its tier must be `synthetic`.

Scenario contracts should be anchored in observable artifacts, not agent trust. Prefer workspace files, transcript terms, outbox authors/statuses, message counts, pause reasons, pane snapshots, or explicit CLI dry-run output. If a scenario only asks an agent to assert that behavior is safe, it is a weak contract and should be hardened before it gates important work.

Real runs write pane snapshot artifacts into the scenario work root so prompt/TUI failures can be audited after cleanup. Prompt delivery experiments should use `scripts/prompt_delivery_smoke.py` before changing relay send-text behavior.

When verifying annotated tags from PowerShell, quote peeled refs so braces are not consumed:

```powershell
git rev-parse --short "v2.0^{}"
```
