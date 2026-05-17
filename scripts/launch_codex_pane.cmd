@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
cd /d "%~3"
echo [agent-mailbox] task=%AGENT_MAILBOX_TASK_ID% role=codex
if "%~4"=="" (
  codex
) else (
  codex "%~4"
)
endlocal
cmd /k
