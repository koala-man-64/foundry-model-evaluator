<#
.SYNOPSIS
    Checks that what Foundry Model Evaluator needs is in place. Installs and changes nothing.

.DESCRIPTION
    You set up the Python environment and build the web interface yourself, from your own package sources.
    This script lists what is in place and what is still needed, with the commands to fix it, and exits with
    code 1 when something is needed. Run it again after pulling new code.

    The Python environment is %LOCALAPPDATA%\FoundryModelEvaluator\runtime\venv unless FME_VENV names another.

.EXAMPLE
    scripts\check.cmd
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Initialize-AppEnvironment
Write-Host "Repository:         $($paths.Repo)"
Write-Host "Data folder:        $($paths.Data)"
Write-Host "Python environment: $($paths.Venv)"
Write-Host ''

$findings = @(Test-AppEnvironment $paths)
Write-Finding $findings
$needed = @($findings | Where-Object { $_.status -eq 'needed' })
Write-Host ''
if ($needed.Count -gt 0) {
    Write-Host "Needed: $($needed.Count). Nothing was installed or changed." -ForegroundColor Red
    exit 1
}
Write-Host 'Everything is in place. Start the app with:  scripts\start.cmd' -ForegroundColor Green
