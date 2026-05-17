@echo off
setlocal
set "AGENT_MAILBOX_TASK_ID=%~1"
set "AGENT_MAILBOX_ROOT=%~2"
if "%AGENT_MAILBOX_CODEX_APPROVAL%"=="" set "AGENT_MAILBOX_CODEX_APPROVAL=never"
if "%AGENT_MAILBOX_CODEX_SANDBOX%"=="" set "AGENT_MAILBOX_CODEX_SANDBOX=danger-full-access"
cd /d "%~3"
echo [agent-mailbox] task=%AGENT_MAILBOX_TASK_ID% role=codex
echo [agent-mailbox] codex_approval=%AGENT_MAILBOX_CODEX_APPROVAL% codex_sandbox=%AGENT_MAILBOX_CODEX_SANDBOX%
if not "%AGENT_MAILBOX_CODEX_EXE%"=="" (
  echo [agent-mailbox] codex_exe=%AGENT_MAILBOX_CODEX_EXE%
  if "%~4"=="" (
    "%AGENT_MAILBOX_CODEX_EXE%" --ask-for-approval "%AGENT_MAILBOX_CODEX_APPROVAL%" --sandbox "%AGENT_MAILBOX_CODEX_SANDBOX%"
  ) else (
    "%AGENT_MAILBOX_CODEX_EXE%" --ask-for-approval "%AGENT_MAILBOX_CODEX_APPROVAL%" --sandbox "%AGENT_MAILBOX_CODEX_SANDBOX%" "%~4"
  )
) else if "%~4"=="" (
  call codex --ask-for-approval "%AGENT_MAILBOX_CODEX_APPROVAL%" --sandbox "%AGENT_MAILBOX_CODEX_SANDBOX%"
) else (
  call codex --ask-for-approval "%AGENT_MAILBOX_CODEX_APPROVAL%" --sandbox "%AGENT_MAILBOX_CODEX_SANDBOX%" "%~4"
)
endlocal
cmd /k
