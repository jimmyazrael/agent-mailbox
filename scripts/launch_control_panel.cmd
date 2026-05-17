@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
cd /d "%~3"
echo [agent-mailbox] task=%AGENT_MAILBOX_TASK_ID% role=control
python "%~4" control-panel --root "%~2" --task-id "%~1"
endlocal
cmd /k
