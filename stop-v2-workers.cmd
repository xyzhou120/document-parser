@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-v2-workers.ps1" %*
set "workerExitCode=%ERRORLEVEL%"
pause
exit /b %workerExitCode%
