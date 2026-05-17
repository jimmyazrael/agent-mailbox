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
set "AGENT_MAILBOX_RELAY_EXIT=%ERRORLEVEL%"
echo [agent-mailbox] relay exited rc=%AGENT_MAILBOX_RELAY_EXIT%
python "%~4" status --root "%~2" --task-id "%~1"
endlocal
cmd /k
