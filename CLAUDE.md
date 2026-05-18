# Claude Code Memory

Canonical agent-mailbox instructions live in `SKILL.md`. Read that file before working in this repository.

## Authorization Protocol

For agent-mailbox development, follow the explicit state model from `SKILL.md`:

```text
DISCUSS -> PROPOSE -> AUTHORIZE -> EXECUTE -> REVIEW -> DONE
```

- Do not implement from critique, agreement, or parked-backlog discussion alone.
- Implementation starts only after the user explicitly authorizes a named subset, such as `Option 2` or `A, B, E`.
- Use one commit per authorized item unless the user explicitly changes that rule.
- After each commit, stop for peer review before starting the next authorized item.
- Treat `DONE` or "standing down" as terminal. New work requires fresh authorization.

Reviewer checklist:

- Diff matches authorized scope.
- Tests pass or gaps are explicitly justified.
- Docs match behavior.
- Names honestly describe what was exercised.
- No silent failure paths were introduced.
- Hard scenario passes are artifact-anchored and exercise what the scenario name implies.
