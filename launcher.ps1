param([switch]$SetupOnly)
$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$appDir = $PSScriptRoot
$dataDir = Join-Path $env:LOCALAPPDATA 'ELRCVideoDownloader'
$runtimeDir = Join-Path $dataDir 'runtime'
$pythonExe = Join-Path $runtimeDir 'Scripts\python.exe'
$pythonwExe = Join-Path $runtimeDir 'Scripts\pythonw.exe'
$requirements = Join-Path $appDir 'requirements.txt'
$stamp = Join-Path $runtimeDir 'requirements.sha256'

function Test-PythonModules([string]$Executable, [string]$Code) {
    $ErrorActionPreference = 'Continue'
    & $Executable -c $Code 2>$null
    return ($LASTEXITCODE -eq 0)
}

try {
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        Write-Host 'Preparing ELRC downloader (first run only)...'
        $candidates = @()
        if ($env:ELRC_PYTHON -and (Test-Path -LiteralPath $env:ELRC_PYTHON)) {
            $candidates += $env:ELRC_PYTHON
        }
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            $ErrorActionPreference = 'Continue'
            $resolved = & $pyLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            $ErrorActionPreference = 'Stop'
            if ($LASTEXITCODE -eq 0) { $candidates += [string]$resolved }
        }
        $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pathPython) { $candidates += $pathPython.Source }
        $basePython = $null
        foreach ($candidate in $candidates) {
            if ($candidate -match 'WindowsApps') { continue }
            if (Test-PythonModules $candidate 'import sys,tkinter,venv; assert sys.version_info >= (3,10)') { $basePython = $candidate; break }
        }
        if (-not $basePython) { throw 'Python 3.10+ with tkinter is required. Install Python from python.org, then run this launcher again.' }
        $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
        $uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
        if (Test-Path -LiteralPath $uvPath) {
            & $uvPath venv --python $basePython $runtimeDir
        } else {
            & $basePython -m venv $runtimeDir
        }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python runtime.' }
    }
    $wanted = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $installed = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { '' }
    $importsOK = Test-PythonModules $pythonExe 'import tkinter,playwright,requests'
    if (-not $importsOK -or $wanted -ne $installed) {
        Write-Host 'Installing downloader dependencies from PyPI...'
        $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
        $uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
        if (Test-Path -LiteralPath $uvPath) {
            & $uvPath pip install --python $pythonExe --link-mode copy -r $requirements
        } else {
            & $pythonExe -m pip install --disable-pip-version-check -r $requirements
        }
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the network and run again.' }
        Set-Content -LiteralPath $stamp -Value $wanted -Encoding Ascii
    }
    if ($SetupOnly) { Write-Host 'ELRC downloader setup verified.'; exit 0 }
    $mainScript = Join-Path $appDir 'elrc_downloader.py'
    Start-Process -FilePath $pythonwExe -ArgumentList @('"' + $mainScript + '"') -WorkingDirectory $appDir -WindowStyle Hidden
} catch {
    Write-Host ('ELRC downloader: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
