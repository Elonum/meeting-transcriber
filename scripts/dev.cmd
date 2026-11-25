@echo off
rem Start the API without touching the PowerShell execution policy.
rem Double-click this file, or run: scripts\dev.cmd [-Build] [-Web] [-NoReload] [-Port 8000]
setlocal
set "SCRIPT=%~dp0dev.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
endlocal
