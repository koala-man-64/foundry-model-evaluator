@echo off
rem Runs start.ps1 without changing the PowerShell execution policy of this computer.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %ERRORLEVEL%
