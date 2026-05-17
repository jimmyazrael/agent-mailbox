@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
if "%AGENT_MAILBOX_CLAUDE_PERMISSION_MODE%"=="" set "AGENT_MAILBOX_CLAUDE_PERMISSION_MODE=auto"
cd /d "%~3"
echo [agent-mailbox] resume task=%AGENT_MAILBOX_TASK_ID% role=claude
echo [agent-mailbox] claude_permission_mode=%AGENT_MAILBOX_CLAUDE_PERMISSION_MODE%
claude --permission-mode "%AGENT_MAILBOX_CLAUDE_PERMISSION_MODE%" --resume "%~4"
endlocal
cmd /k
