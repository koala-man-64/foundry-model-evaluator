<#
.SYNOPSIS
    Runs the foundry-eval command line (pricing checks, smoke tests, ...).

.EXAMPLE
    scripts\foundry-eval.cmd pricing-check --profile "GPT-5.6 terra"

.EXAMPLE
    scripts\foundry-eval.cmd smoke --profile "GPT-5.6 terra" --budget 0.05
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Initialize-AppEnvironment
$exe = Get-VenvTool $paths 'foundry-eval.exe'
if (-not (Test-Path $exe)) {
    Write-Host 'Foundry Model Evaluator is not installed yet. Run scripts\setup.cmd first.' -ForegroundColor Red
    exit 1
}
& $exe @args
exit $LASTEXITCODE
