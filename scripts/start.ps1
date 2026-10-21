<#
.SYNOPSIS
    Starts Foundry Model Evaluator and opens it in the browser.

.DESCRIPTION
    Serves the web interface and the local API on http://127.0.0.1:<port> (loopback only).
    Press Ctrl+C to stop. It first checks what it needs, like scripts\check.cmd, and does not start when
    something is needed.

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
Assert-AppEnvironment $paths
$env:FME_STATIC_DIR = Get-WebInterfaceDir $paths
$exe = Get-VenvTool $paths 'foundry-eval.exe'
$arguments = @('serve', '--port', "$Port")
if ($NoBrowser) { $arguments += '--no-browser' }
& $exe @arguments
exit $LASTEXITCODE
