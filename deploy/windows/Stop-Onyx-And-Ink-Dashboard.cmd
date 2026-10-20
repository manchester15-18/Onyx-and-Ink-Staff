@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
set "result=%ERRORLEVEL%"
if not "%result%"=="0" (
  echo.
  echo Dashboard did not stop cleanly. Review the message above.
  pause
)
exit /b %result%
