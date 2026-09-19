<#
.SYNOPSIS
    Build the clockwork Windows executable and measure its startup time.
.DESCRIPTION
    Drives PyInstaller against packaging/clockwork.spec (dist/ and build/ land at the
    repo root regardless of caller cwd), then launches the built .exe twice -- timing
    from process start to the Qt window appearing -- so cold and warm startup are both
    on record. The build is onedir only: a clockwork/ folder holding
    clockwork.exe beside its dependencies, no extraction, packaged by Inno Setup
    (packaging/clockwork.iss) -- mainspring measured the alternative, a single .exe that
    extracts to a temp directory on every launch, unacceptably slow (mainspring's
    task 07), and clockwork inherits that chain rather than remeasuring it.
.PARAMETER SkipBuild
    Measure startup against whatever is already in dist/ without rebuilding.
#>
param(
    [switch]$SkipBuild,
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$distDir = Join-Path $root "dist"
$buildDir = Join-Path $root "build"
$exePath = Join-Path $distDir "clockwork\clockwork.exe"

if (-not $SkipBuild) {
    Write-Host "Warming the numba kernel cache for packaging/numba_cache_seed..." -ForegroundColor Cyan
    uv run tools/warm_numba_cache.py
    if ($LASTEXITCODE -ne 0) {
        throw "Warming the numba cache failed (exit $LASTEXITCODE)."
    }

    Write-Host "Recording the commit this build is built from..." -ForegroundColor Cyan
    # Gitignored, rewritten before every build; left in place afterwards, like the
    # numba seed above (lab record, task 32; mirrors mainspring's task 20).
    uv run tools/write_commit.py
    if ($LASTEXITCODE -ne 0) {
        throw "Recording the build commit failed (exit $LASTEXITCODE)."
    }

    Write-Host "Staging the acquisition console payload..." -ForegroundColor Cyan
    # Never fails the build: a checkout with no console build yet (or a public clone)
    # gets a clockwork.exe with no console beside it, and says so (lab record, task 52).
    uv run tools/stage_console.py
    if ($LASTEXITCODE -ne 0) {
        throw "Staging the console payload failed (exit $LASTEXITCODE)."
    }

    Write-Host "Building clockwork.exe with PyInstaller..." -ForegroundColor Cyan
    # Build with only the Windows directories and uv's on PATH. PyInstaller resolves DLL
    # dependencies through PATH as a last resort, so a PATH carrying another Python
    # distribution makes the build depend on the shell it ran from (mainspring's task 07:
    # anaconda3/Library/bin's ICU once shadowed Windows's own and every launch died
    # importing QtCore). The spec's provenance guard fails the build on anything this
    # misses.
    $savedPath = $env:PATH
    $uvDir = Split-Path -Parent (Get-Command uv).Source
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem;$uvDir"
    try {
        uv run pyinstaller packaging/clockwork.spec --distpath $distDir --workpath $buildDir --noconfirm --clean
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller build failed (exit $LASTEXITCODE)."
        }
    } finally {
        $env:PATH = $savedPath
    }
}

if (-not (Test-Path $exePath)) {
    throw "Expected build output at $exePath, but it does not exist. Run without -SkipBuild first."
}

$consolePayload = Join-Path $root "packaging\console_payload"
$consoleDest = Join-Path $distDir "clockwork\console"
if ((Test-Path $consolePayload) -and (Get-ChildItem $consolePayload -ErrorAction SilentlyContinue)) {
    Write-Host "Copying the console payload beside clockwork.exe..." -ForegroundColor Cyan
    # A plain directory copy, not a PyInstaller datas entry: PyInstaller's own onedir
    # layout nests bundled data under dist\clockwork\_internal\, and
    # clockwork.acq.find_console looks for console\ beside the .exe itself (lab
    # record, task 50 decision 9) -- so this has to land one level up from where
    # collect_data_files() would have put it.
    New-Item -ItemType Directory -Force -Path $consoleDest | Out-Null
    Copy-Item -Path (Join-Path $consolePayload "*") -Destination $consoleDest -Force -Recurse
} else {
    Write-Host "No console payload staged -- clockwork.exe will ship without the acquisition console." -ForegroundColor Yellow
}

Write-Host "Running --self-check against the built .exe..." -ForegroundColor Cyan
# A windowed exe (console=False, task 60) returns control to PowerShell the instant it
# is started, so `& $exePath --self-check; $LASTEXITCODE` would read the exit code of
# nothing -- clockwork.app.main attaches to this console on its own (AttachConsole) and
# prints here, but the process itself still has to be waited on.
$selfCheck = Start-Process -FilePath $exePath -ArgumentList "--self-check" -Wait -PassThru
if ($selfCheck.ExitCode -ne 0) {
    throw "clockwork.exe --self-check failed (exit $($selfCheck.ExitCode)) -- see its own output above."
}

$size = (Get-ChildItem (Split-Path $exePath) -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host ("clockwork/ folder size: {0:N1} MB" -f ($size / 1MB))
if (Test-Path $consoleDest) {
    $consoleSize = (Get-ChildItem $consoleDest -Recurse | Measure-Object -Property Length -Sum).Sum
    Write-Host ("  of which console/: {0:N1} MB" -f ($consoleSize / 1MB))
}

function Measure-Startup([string]$label) {
    $proc = Start-Process -FilePath $exePath -PassThru
    $start = Get-Date
    $deadline = $start.AddSeconds($TimeoutSeconds)
    # Windowed (console=False, task 60), the same shape as mainspring's own build: one
    # window, the Qt one, so MainWindowHandle alone is "the app is up" -- no console to
    # race against and no placeholder title to wait past.
    while ((-not $proc.HasExited) -and ($proc.MainWindowHandle -eq [IntPtr]::Zero)) {
        Start-Sleep -Milliseconds 25
        $proc.Refresh()
        if ((Get-Date) -gt $deadline) {
            if (-not $proc.HasExited) { $proc | Stop-Process -Force }
            throw "$label startup: window never appeared within $TimeoutSeconds s."
        }
    }
    if ($proc.HasExited) {
        throw "$label startup: process exited before showing a window (exit $($proc.ExitCode))."
    }
    $elapsed = (Get-Date) - $start
    # PyInstaller's "Unhandled exception in script" crash dialog is a top-level window
    # too, and a build that died on launch once passed this check on its title alone
    # (mainspring's task 07).
    $title = $proc.MainWindowTitle
    if ($title -notlike "clockwork*") {
        $proc | Stop-Process -Force
        throw "$label startup: the first window was '$title', not clockwork -- the build crashed on launch."
    }
    Write-Host ("{0} startup: {1:N2} s" -f $label, $elapsed.TotalSeconds)
    $proc | Stop-Process -Force
    return $elapsed.TotalSeconds
}

$cold = Measure-Startup "Cold"
Start-Sleep -Seconds 1
$warm = Measure-Startup "Warm"

Write-Host ""
Write-Host ("Cold {0:N2} s / Warm {1:N2} s" -f $cold, $warm) -ForegroundColor Green
Write-Host "Record these in the lab record if the numbers move."
