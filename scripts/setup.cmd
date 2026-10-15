@echo off
rem Runs setup.ps1 without changing the PowerShell execution policy of this computer.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
