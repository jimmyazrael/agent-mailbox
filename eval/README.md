# Agent Mailbox Eval Scenarios

These are real-agent behavioral scenarios for `agent-mailbox`. They are intentionally hard and run against real Claude/Codex/WezTerm in temp workspaces.

Run one scenario:

```powershell
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
python scripts/mailbox_eval.py --scenario AM-01
```

Run all scenarios:

```powershell
$env:AGENT_MAILBOX_RUN_REAL_SMOKE = "1"
python scripts/mailbox_eval.py
```

The runner creates temp project directories and mailbox roots. It may open WezTerm panes, create Claude/Codex conversations, and use tokens. It must not modify important project files.
