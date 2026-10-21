# Shared helpers for the Foundry Model Evaluator scripts (Windows PowerShell 5.1 and PowerShell 7).
# The scripts never install or change anything: you set up the Python environment and build the web
# interface yourself, from your own package sources, and the scripts check what is there.

Set-StrictMode -Version Latest

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
    $venv = Get-EnvValue 'FME_VENV'
    if (-not $venv) { $venv = Join-Path $runtime 'venv' }
    return @{
        Repo     = $repo
        Backend  = Join-Path $repo 'backend'
        Frontend = Join-Path $repo 'frontend'
        Data     = $dataDir
        Venv     = [System.IO.Path]::GetFullPath($venv)
        Ui       = Join-Path $runtime 'ui'
    }
}

function Get-VenvTool {
    param([hashtable]$Paths, [string]$Name)
    return Join-Path (Join-Path $Paths.Venv 'Scripts') $Name
}

# The built web interface, in the order foundry_eval.paths.default_static_dir uses:
# FME_STATIC_DIR, then frontend\dist, then runtime\ui.
function Get-WebInterfaceDir {
    param([hashtable]$Paths)
    $override = Get-EnvValue 'FME_STATIC_DIR'
    if ($override) { return [System.IO.Path]::GetFullPath($override) }
    foreach ($candidate in @((Join-Path $Paths.Frontend 'dist'), $Paths.Ui)) {
        if (Test-Path (Join-Path $candidate 'index.html')) { return $candidate }
    }
    return $null
}

function Get-NodeStatus {
    if (-not (Get-Command 'node' -ErrorAction SilentlyContinue)) { return 'Node.js was not found' }
    $version = [version]((& node -p 'process.versions.node').Trim())
    if ($version -lt $script:MinNodeVersion) { return "found Node.js $version, which is too old" }
    return "found Node.js $version"
}

function Test-PythonEnvironment {
    param([hashtable]$Paths)
    $python = Get-VenvTool $Paths 'python.exe'
    $install = "`"$python`" -m pip install -e `"$($Paths.Backend)`""
    if (-not (Test-Path $python)) {
        return [pscustomobject]@{
            status = 'needed'
            title  = 'A Python virtual environment'
            detail = "None at $($Paths.Venv). Create one with Python 3.12 or newer (not the Microsoft Store " +
                "Python) and install the backend into it in editable mode, for example:`n" +
                "  python -m venv `"$($Paths.Venv)`"`n  $install`n" +
                'To use an environment somewhere else, set FME_VENV to its folder.'
        }
    }
    # Run with the environment's own Python, which is what the app will run on.
    $checker = Join-Path $Paths.Backend 'src\foundry_eval\environment_check.py'
    try {
        $output = & $python -I $checker --backend $Paths.Backend
        $code = $LASTEXITCODE
        if ($code -gt 1) { throw "exit code $code" }
        $findings = @((($output -join "`n") | ConvertFrom-Json).findings)
    } catch {
        return [pscustomobject]@{
            status = 'needed'
            title  = 'A working Python environment'
            detail = "Could not run $python ($(($_.Exception.Message -replace '(?s)At \S+:\d+ char:\d+.*$', '').Trim())). " +
                'Create the environment again.'
        }
    }
    $missingCommand = -not (Test-Path (Get-VenvTool $Paths 'foundry-eval.exe'))
    if ($missingCommand -and -not @($findings | Where-Object { $_.status -eq 'needed' })) {
        $findings += [pscustomobject]@{
            status = 'needed'
            title  = 'The foundry-eval command'
            detail = "Not found in $($Paths.Venv)\Scripts. Install the backend again: $install"
        }
    }
    return $findings
}

function Get-BuildHint {
    param([hashtable]$Paths)
    return "Build it with Node.js $($script:MinNodeVersion) or newer ($(Get-NodeStatus)):`n" +
        "  cd `"$($Paths.Frontend)`"`n  npm ci`n  npm run build`n" +
        'Or build a copy outside OneDrive and set FME_STATIC_DIR to its dist folder.'
}

function Test-WebInterface {
    param([hashtable]$Paths)
    $ui = Get-WebInterfaceDir $Paths
    if (-not $ui) {
        return [pscustomobject]@{
            status = 'needed'
            title  = 'The web interface, built'
            detail = "Not found in $(Join-Path $Paths.Frontend 'dist') or $($Paths.Ui). $(Get-BuildHint $Paths)"
        }
    }
    $index = Join-Path $ui 'index.html'
    if (-not (Test-Path $index)) {
        return [pscustomobject]@{
            status = 'needed'
            title  = 'The web interface, built'
            detail = "FME_STATIC_DIR is set to $ui, which has no index.html. $(Get-BuildHint $Paths)"
        }
    }
    $findings = @([pscustomobject]@{ status = 'ok'; title = 'Web interface built'; detail = $ui })
    # After a pull, sources newer than the build mean it needs building again.
    $sources = @(Get-ChildItem -Path (Join-Path $Paths.Frontend 'src') -Recurse -File -ErrorAction SilentlyContinue)
    $sources += @(Get-ChildItem -Path (Join-Path $Paths.Frontend '*') -File -ErrorAction SilentlyContinue `
            -Include 'index.html', 'package*.json', 'vite.config.*')
    $newest = $sources | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($newest -and $newest.LastWriteTimeUtc -gt (Get-Item $index).LastWriteTimeUtc) {
        $findings += [pscustomobject]@{
            status = 'warning'
            title  = 'The web interface may be out of date'
            detail = "$($newest.Name) changed after it was built. $(Get-BuildHint $Paths)"
        }
    }
    return $findings
}

# What is in place and what is needed: objects with status ('ok', 'needed' or 'warning'), title and detail.
function Test-AppEnvironment {
    param([hashtable]$Paths, [switch]$SkipWebInterface)
    $findings = @(Test-PythonEnvironment $Paths)
    if (-not $SkipWebInterface) { $findings += @(Test-WebInterface $Paths) }
    return $findings
}

function Write-Finding {
    param([object[]]$Findings)
    $colors = @{ ok = 'Green'; needed = 'Red'; warning = 'Yellow' }
    foreach ($finding in $Findings) {
        Write-Host ('  {0,-10}' -f "[$($finding.status)]") -ForegroundColor $colors[$finding.status] -NoNewline
        Write-Host $finding.title
        if ($finding.detail) {
            foreach ($line in ($finding.detail -split "`n")) { Write-Host "            $line" }
        }
    }
}

# For start.ps1 and foundry-eval.ps1: shows only what is not in place, and exits when something is needed.
function Assert-AppEnvironment {
    param([hashtable]$Paths, [switch]$SkipWebInterface)
    $problems = @(Test-AppEnvironment $Paths -SkipWebInterface:$SkipWebInterface | Where-Object { $_.status -ne 'ok' })
    if ($problems.Count -eq 0) { return }
    Write-Finding $problems
    if (@($problems | Where-Object { $_.status -eq 'needed' }).Count -gt 0) {
        Write-Host ''
        Write-Host 'Something is needed first; scripts\check.cmd lists everything.' -ForegroundColor Red
        exit 1
    }
}
