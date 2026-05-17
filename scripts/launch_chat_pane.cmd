@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
cd /d "%~3"
echo [agent-mailbox] task=%AGENT_MAILBOX_TASK_ID% role=chat
if "%~5"=="" (
  python "%~4" watch-chat --root "%~2" --task-id "%~1"
) else (
  python "%~4" watch-chat --root "%~2" --task-id "%~1" --poll-interval-s "%~5"
)
endlocal
cmd /k
