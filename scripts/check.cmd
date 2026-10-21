@echo off
rem Runs check.ps1 without changing the PowerShell execution policy of this computer.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0check.ps1" %*
exit /b %ERRORLEVEL%
