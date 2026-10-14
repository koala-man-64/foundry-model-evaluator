# Shared helpers for the Foundry Model Evaluator scripts (Windows PowerShell 5.1 and PowerShell 7).
# Everything the scripts install lives under %LOCALAPPDATA%\FoundryModelEvaluator\runtime,
# outside the (often OneDrive-synced) repository.

Set-StrictMode -Version Latest

$script:PythonVersion = '3.13'
$script:MinNodeVersion = [version]'20.19.0'

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Test-UnderPath {
    param([string]$Child, [string]$Parent)
    if (-not $Parent) { return $false }
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $c = [System.IO.Path]::GetFullPath($Child).TrimEnd($sep) + $sep
    $p = [System.IO.Path]::GetFullPath($Parent).TrimEnd($sep) + $sep
    return $c.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-EnvValue {
    param([string]$Name)
    return [Environment]::GetEnvironmentVariable($Name)
}

function Initialize-AppEnvironment {
    $override = Get-EnvValue 'FME_DATA_DIR'
    $localAppData = Get-EnvValue 'LOCALAPPDATA'
    if ($override) {
        $dataDir = $override
    } elseif ($localAppData) {
        $dataDir = Join-Path $localAppData 'FoundryModelEvaluator'
    } else {
        throw 'LOCALAPPDATA is not set. Set FME_DATA_DIR to a folder outside OneDrive and the repository.'
    }
    $repo = Get-RepoRoot
    foreach ($name in @('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial')) {
        $oneDrive = Get-EnvValue $name
        if (Test-UnderPath $dataDir $oneDrive) {
            throw "The data folder $dataDir is inside OneDrive ($oneDrive). Set FME_DATA_DIR to a local folder."
        }
    }
    if (Test-UnderPath $dataDir $repo) {
        throw "The data folder $dataDir is inside the repository. Set FME_DATA_DIR to a folder outside it."
    }
    $runtime = Join-Path $dataDir 'runtime'
    $paths = @{
        Repo    = $repo
        Backend = Join-Path $repo 'backend'
        Data    = $dataDir
        Runtime = $runtime
        Venv    = Join-Path $runtime 'venv'
        Python  = Join-Path $runtime 'python'
        Build   = Join-Path $runtime 'build'
        Ui      = Join-Path $runtime 'ui'
    }
    # uv: keep the interpreter and the virtual environment out of the repository, and never use the
    # Microsoft Store Python (it redirects writes under %LOCALAPPDATA%).
    $env:UV_PROJECT_ENVIRONMENT = $paths.Venv
    $env:UV_PYTHON_INSTALL_DIR = $paths.Python
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:UV_PYTHON = $script:PythonVersion
    return $paths
}

function Assert-Command {
    param([string]$Name, [string]$Help)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Host ''
        Write-Host "'$Name' was not found." -ForegroundColor Red
        Write-Host $Help
        exit 1
    }
}

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Robocopy {
    param([string]$Source, [string]$Destination, [string[]]$ExcludeDirs = @())
    $arguments = @($Source, $Destination, '/MIR', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:2', '/W:1')
    if ($ExcludeDirs.Count -gt 0) { $arguments += @('/XD') + $ExcludeDirs }
    & robocopy.exe @arguments | Out-Null
    # robocopy exit codes below 8 mean success (files copied, extra files removed, ...).
    if ($LASTEXITCODE -ge 8) { throw "robocopy $Source -> $Destination failed with exit code $LASTEXITCODE." }
    $global:LASTEXITCODE = 0
}

function Get-VenvTool {
    param([hashtable]$Paths, [string]$Name)
    return Join-Path (Join-Path $Paths.Venv 'Scripts') $Name
}
