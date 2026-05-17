# Agent Mailbox Control Panel Design

## Goal

Give the user simple in-WezTerm controls for common accident handling while a mailbox task is running:

- status refresh
- pause
- resume
- inject guidance
- bounce/restart an existing agent pane
- rediscover Codex
- stop

The user should not need to return to README.md or remember `python scripts\mailbox.py ...` commands during a risky task.

## WezTerm Research Summary

WezTerm supports:

- CLI operations such as `wezterm cli spawn`, `split-pane`, `send-text`, `get-text`, and `list`.
- Lua configuration and plugins loaded from the user's WezTerm config.
- Key bindings, launch menu entries, and custom status rendering from Lua config.

Important limitation:

- WezTerm plugins/config are not a good per-task dynamic surface unless the user has already installed a global loader in their WezTerm config. Agent Mailbox should not silently edit global WezTerm config for every task.

Implication:

- The first implementation should be a mailbox-owned terminal control panel launched in a WezTerm pane/tab, not a WezTerm Lua plugin.
- A WezTerm Lua integration can be a later optional enhancement.

## Proposed V1.2 Shape

Add a read/write terminal UI:

```powershell
python scripts\mailbox.py control-panel --root <root> --task-id <id>
```

Default task launch should open a fifth WezTerm view:

```text
Claude | Codex | relay | chat transcript | control panel
```

The control panel is a simple Python loop. It prints current state and accepts one-key commands.

## Control Panel Commands

Suggested keys:

```text
r  refresh status
p  pause relay
c  continue / resume
i  inject guidance
a  agent actions / bounce submenu
d  rediscover Codex session
s  stop task, keep panes open
q  quit control panel only
?  help
```

Agent actions submenu:

```text
1  bounce Claude in existing pane
2  bounce Codex in existing pane
3  rediscover Codex, then bounce Codex if discovered
4  run resume to recreate missing panes
5  rebind pane id manually
q  back
```

The panel should display:

- task id
- status
- turn
- last message id
- round
- paused flag and pause reason
- bound panes
- Codex discovery status
- latest message summary

## Safety Rules

- `s` stops automation but should not close panes.
- `i` must show the target and message before sending.
- Agent bounce should pause the relay first, restart the selected agent, then ask whether to resume.
- Bounce confirmations are conservative: prompt to pause with default yes, then prompt to restart with default no, then prompt to resume with default yes after a successful restart.
- Bouncing Claude uses the stored Claude session id via `repair --restart-agent claude`.
- Bouncing Codex uses the stored Codex session id via `repair --restart-agent codex`.
- If Codex session id is missing, offer `repair --rediscover-codex` first. If rediscovery fails, explain that Codex may need to post once with the task marker or be manually rebound.
- Manual pane rebind must validate the pane id against live `wezterm cli --prefer-mux list` panes in the task workspace before invoking `repair --rebind-pane`.
- The panel must never bypass mailbox APIs. Status reads should use read-only DB access. Writes should call the existing `mailbox.py` subcommands via subprocess (`pause`, `resume`, `inject`, `stop`, `repair`).
- If DB is locked or unavailable, show the error and keep the panel alive.
- If a task is terminal, show terminal status and allow `q`, `r`, and transcript viewing only.
- Do not expose "stop and close panes" in the keymap. If the user wants panes closed, use the explicit CLI command: `mailbox.py stop --close-panes --yes`.

## Launch Integration

Add:

```text
scripts/launch_control_panel.cmd
mailbox.py control-panel
mailbox.py start --no-control-panel
mailbox.py launch-tui --no-control-panel
```

Default behavior:

- `start` and `launch-tui` open the control panel by default.
- `--no-control-panel` disables it.
- `status` reports `panes.control`.

The existing default-on chat behavior stays unchanged.

## Layout Recommendation

Keep the existing Claude/Codex split and relay pane behavior.

Launch chat and control panel as tabs in the same WezTerm window using the fixed `--window-id` path. Tabs are better than crowding the Claude/Codex panes.

## Testing Plan

Unit tests:

- parser exposes `control-panel`, `--no-control-panel`, and default control enabled.
- fake-pane start binds `panes.control`.
- `--no-control-panel` does not bind `panes.control`.
- control-panel command can render status once with `--max-iters 1`.
- pause/resume/inject/stop actions update the DB as expected via panel handlers.
- bounce Claude/Codex actions issue `repair --restart-agent <agent>` through subprocess.
- Codex bounce without session id offers/uses `repair --rediscover-codex` first.
- no close-panes action is exposed in the panel keymap.

Real/no-side-effect smoke:

- Start an eval scenario with control panel enabled.
- Verify `panes.control` exists.
- Run a control-panel one-shot status render against the kept mailbox DB.

## Deferred Optional WezTerm Lua Plugin

A future optional plugin could add WezTerm-native key bindings or launch menu entries.

Do not implement this first because:

- It requires global WezTerm config changes.
- Dynamic per-task plugin loading is not a reliable assumption.
- The Python panel gives the user the needed buttons/commands without risking their global terminal setup.
