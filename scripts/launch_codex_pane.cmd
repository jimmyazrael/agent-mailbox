@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
cd /d "%~3"
echo [agent-mailbox] task=%AGENT_MAILBOX_TASK_ID% role=codex
if not "%AGENT_MAILBOX_CODEX_EXE%"=="" (
  echo [agent-mailbox] codex_exe=%AGENT_MAILBOX_CODEX_EXE%
  if "%~4"=="" (
    "%AGENT_MAILBOX_CODEX_EXE%"
  ) else (
    "%AGENT_MAILBOX_CODEX_EXE%" "%~4"
  )
) else if "%~4"=="" (
  call codex
) else (
  call codex "%~4"
)
endlocal
cmd /k
