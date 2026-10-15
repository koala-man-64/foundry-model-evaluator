<#
.SYNOPSIS
    Installs or updates Foundry Model Evaluator on Windows.

.DESCRIPTION
    Installs a uv-managed Python, the backend and its dependencies, and builds the web interface.
    Everything goes under %LOCALAPPDATA%\FoundryModelEvaluator\runtime, outside the repository,
    so nothing large or machine-specific is synced by OneDrive. Run it again after pulling new code.

.PARAMETER Dev
    Also install the development tools (tests, linters, type checker).

.PARAMETER SkipFrontend
    Do not rebuild the web interface (for example when Node.js is not installed yet).

.EXAMPLE
    scripts\setup.cmd
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Initialize-AppEnvironment
Write-Host "Repository:  $($paths.Repo)"
Write-Host "Data folder: $($paths.Data)"
New-Item -ItemType Directory -Force -Path $paths.Runtime | Out-Null

Assert-Command 'uv' @'
Install uv (the Python package manager) first, then open a new terminal and run this script again:
    winget install --id=astral-sh.uv -e
or see https://docs.astral.sh/uv/getting-started/installation/
'@

Write-Host ''
Write-Host "Installing Python $script:PythonVersion (managed by uv)..." -ForegroundColor Cyan
Invoke-Checked 'uv' @('python', 'install', $script:PythonVersion)

Write-Host ''
Write-Host 'Installing the backend...' -ForegroundColor Cyan
$syncArgs = @('sync', '--locked', '--project', $paths.Backend)
if (-not $Dev) { $syncArgs += '--no-dev' }
Invoke-Checked 'uv' $syncArgs

if ($SkipFrontend) {
    Write-Host ''
    Write-Host 'Skipping the web interface build (-SkipFrontend).' -ForegroundColor Yellow
} else {
    Assert-Command 'node' @'
The web interface is built with Node.js. Install the LTS version, open a new terminal and run this
script again (or pass -SkipFrontend):
    winget install --id=OpenJS.NodeJS.LTS -e
'@
    $nodeVersion = [version]((& node -p 'process.versions.node').Trim())
    if ($nodeVersion -lt $script:MinNodeVersion) {
        throw "Node.js $nodeVersion is too old; version $($script:MinNodeVersion) or newer is required."
    }
    Write-Host ''
    Write-Host 'Building the web interface...' -ForegroundColor Cyan
    # Build from a copy outside the repository so node_modules never lands in OneDrive.
    $buildDir = Join-Path $paths.Build 'frontend'
    Invoke-Robocopy (Join-Path $paths.Repo 'frontend') $buildDir @(
        'node_modules', 'dist', 'test-results', 'playwright-report', 'blob-report'
    )
    Push-Location $buildDir
    try {
        Invoke-Checked 'npm' @('ci', '--no-audit', '--no-fund')
        Invoke-Checked 'npm' @('run', 'build')
    } finally {
        Pop-Location
    }
    Invoke-Robocopy (Join-Path $buildDir 'dist') $paths.Ui
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host 'Start the app with:  scripts\start.cmd'
