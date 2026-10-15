<#
.SYNOPSIS
    Starts Foundry Model Evaluator and opens it in the browser.

.DESCRIPTION
    Serves the web interface and the local API on http://127.0.0.1:<port> (loopback only).
    Press Ctrl+C to stop. Run scripts\setup.cmd first.

.PARAMETER Port
    Local port (default 8765).

.PARAMETER NoBrowser
    Do not open the browser.

.EXAMPLE
    scripts\start.cmd
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Initialize-AppEnvironment
$exe = Get-VenvTool $paths 'foundry-eval.exe'
if (-not (Test-Path $exe)) {
    Write-Host 'Foundry Model Evaluator is not installed yet. Run scripts\setup.cmd first.' -ForegroundColor Red
    exit 1
}
if (-not (Get-EnvValue 'FME_STATIC_DIR') -and (Test-Path (Join-Path $paths.Ui 'index.html'))) {
    $env:FME_STATIC_DIR = $paths.Ui
}
$arguments = @('serve', '--port', "$Port")
if ($NoBrowser) { $arguments += '--no-browser' }
& $exe @arguments
exit $LASTEXITCODE
