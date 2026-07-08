@echo off
setlocal enableextensions

REM claude-glm.cmd - Windows batch shim for the Claude Code-compatible GLM
REM wrapper. It routes claude through GLM-compatible auth (api.z.ai) and keeps
REM the same runtime identity (claude-glm) / provider (z.ai) as the POSIX
REM claude-glm.sh twin.

set "_first=%~1"

REM Delegate version/help to claude with NO key check and NO GLM env,
REM matching claude-glm.sh (token check is skipped for these args).
if "%_first%"=="--version" goto :delegate
if "%_first%"=="version"    goto :delegate
if "%_first%"=="--help"     goto :delegate
if "%_first%"=="help"       goto :delegate
if "%_first%"=="-h"         goto :delegate

REM Resolve the token from the process environment only. A dependency-free
REM native Windows Credential Manager read is not available from a batch
REM file, so the persistent PowerShell option below is the documented path.
if not defined ANTHROPIC_AUTH_TOKEN goto :missing

REM GLM-compatible environment (same defaults as claude-glm.sh).
set "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic"
set "ANTHROPIC_DEFAULT_HAIKU_MODEL=glm-4.7"
set "ANTHROPIC_DEFAULT_SONNET_MODEL=glm-5.2[1m]"
set "ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.2[1m]"
set "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
set "API_TIMEOUT_MS=3000000"

:delegate
REM Hand off to claude with all args. Invoking the claude shim without
REM `call` replaces this wrapper (the batch analog of `exec claude`), and the
REM child inherits the GLM environment set above. (`goto :eof` covers the
REM claude.exe case where control returns to this script.)
claude %*
goto :eof

:missing
echo claude-glm: missing GLM Coding Plan key. 1>&2
echo. 1>&2
echo Auth source precedence: 1>&2
echo   1. ANTHROPIC_AUTH_TOKEN in the process environment. 1>&2
echo   2. Windows has no batch-native credential read; use the persistent option below. 1>&2
echo. 1>&2
echo Set it for this process from PowerShell: 1>&2
echo   powershell -Command "$env:ANTHROPIC_AUTH_TOKEN = \"^<GLM Coding Plan key^>\"; claude-glm --print --model \"glm-5.2[1m]\" \"Reply OK only.\"" 1>&2
echo. 1>&2
echo Or persist it for future shells (then restart your shell for it to take effect): 1>&2
echo   powershell -Command "[Environment]::SetEnvironmentVariable(\"ANTHROPIC_AUTH_TOKEN\", \"^<GLM Coding Plan key^>\", \"User\")" 1>&2
echo. 1>&2
echo Note: Claude Code may warn that "claude.ai connectors are disabled because ANTHROPIC_API_KEY 1>&2
echo or another auth source is set...". This is expected for claude-glm, which intentionally 1>&2
echo routes through GLM-compatible auth. 1>&2
exit /b 2
