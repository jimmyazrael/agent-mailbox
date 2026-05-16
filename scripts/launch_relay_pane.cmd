@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
cd /d "%~3"
echo [agent-mailbox] task=%AGENT_MAILBOX_TASK_ID% role=relay
if "%~5"=="" (
  python "%~4" tui-relay --root "%~2" --task-id "%~1"
) else (
  python "%~4" tui-relay --root "%~2" --task-id "%~1" --max-iters "%~5"
)
endlocal
cmd /k
