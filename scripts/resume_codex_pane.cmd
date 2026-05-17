@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
cd /d "%~3"
echo [agent-mailbox] resume task=%AGENT_MAILBOX_TASK_ID% role=codex
codex resume "%~4"
endlocal
cmd /k
