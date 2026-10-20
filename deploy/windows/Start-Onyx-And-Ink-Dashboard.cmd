@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "result=%ERRORLEVEL%"
if not "%result%"=="0" (
  echo.
  echo Dashboard did not start. Review the message above.
  pause
)
exit /b %result%
