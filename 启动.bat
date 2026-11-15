@echo off
rem ===========================================================================
rem  VAIR one-click launcher (Windows).
rem
rem  All real logic lives in start.ps1 -- read that file for comments (Chinese).
rem  This shell is deliberately ASCII-only and does nothing but call PowerShell:
rem  cmd.exe decodes a .bat using whatever console codepage it is in, and
rem  goto / call :label re-read the file by byte offset. Mixing Chinese text,
rem  "chcp 65001" and labels in one .bat makes cmd split lines at the wrong
rem  byte offsets (it prints a pile of "'xxx' is not recognized as an internal
rem  or external command"). Keeping this file ASCII avoids the whole class of
rem  problem -- ASCII is one byte in every codepage.
rem
rem  Usage:  double-click, or from a terminal:  start.bat --lan
rem  Extra arguments are forwarded to 后端\run.py (--port, --selfcheck, ...).
rem ===========================================================================
setlocal

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "CODE=%ERRORLEVEL%"

rem Keep the window when something went wrong, or when it was double-clicked
rem (a console opened by Explorer would otherwise vanish before it can be read).
if not "%CODE%"=="0" (
  echo.
  pause
  exit /b %CODE%
)
if "%~1"=="" pause
exit /b 0
