@echo off
rem Runs foundry-eval.ps1 without changing the PowerShell execution policy of this computer.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0foundry-eval.ps1" %*
exit /b %ERRORLEVEL%
