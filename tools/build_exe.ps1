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
    Write-Host "Recording the commit this build is built from..." -ForegroundColor Cyan
    # Gitignored, rewritten before every build; left in place afterwards (lab record,
    # task 32; mirrors mainspring's task 20).
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

    Write-Host "Seeding the build with its own compiled numba kernels..." -ForegroundColor Cyan
    # After PyInstaller, from the built .exe: numba stamps a frozen program's cache with the
    # executable, so only the executable can write a seed an installed copy will read
    # (mainspring's lab record, task 34).
    uv run tools/warm_numba_cache.py
    if ($LASTEXITCODE -ne 0) {
        throw "Seeding the numba cache failed (exit $LASTEXITCODE)."
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

function Invoke-SelfCheck([int]$attempt) {
    # A windowed exe (console=False, task 60) returns control to PowerShell the instant it
    # is started, so `& $exePath --self-check; $LASTEXITCODE` would read the exit code of
    # nothing: the process has to be waited on. Whether it then attaches to this console
    # is not predictable from how it was started, and a console a backgrounded session
    # cannot read lost the report of two failed first runs (lab record, task 84); so the
    # report also goes to a file under build\ -- never %LOCALAPPDATA%, which a process
    # started from a packaged app sees virtualized -- and is printed from there.
    $report = Join-Path $buildDir "self-check-$attempt.log"
    Remove-Item $report -Force -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath $exePath -Wait -PassThru `
        -ArgumentList "--self-check", "--self-check-report", "`"$report`""
    Write-Host "--self-check attempt $attempt exited $($proc.ExitCode); report $report"
    if (Test-Path $report) {
        Get-Content $report | ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Host "    (no report written: the exe stopped before its self-check began)" -ForegroundColor Yellow
    }
    return $proc.ExitCode
}

Write-Host "Running --self-check against the built .exe..." -ForegroundColor Cyan
$firstExit = Invoke-SelfCheck 1
if ($firstExit -ne 0) {
    # Run once more, so a first launch that fails and a second that passes is reported as
    # exactly that rather than as a broken build; both reports are kept under build\.
    Write-Host "The first self-check failed; running it again..." -ForegroundColor Yellow
    $secondExit = Invoke-SelfCheck 2
    if ($secondExit -ne 0) {
        throw "clockwork.exe --self-check failed twice (exit $firstExit, then $secondExit) -- reports above."
    }
    Write-Host "FIRST SELF-CHECK FAILED (exit $firstExit), SECOND PASSED -- a first-run failure, not a broken build; record build\self-check-1.log." -ForegroundColor Yellow
}

$size = (Get-ChildItem (Split-Path $exePath) -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host ("clockwork/ folder size: {0:N1} MB" -f ($size / 1MB))
if (Test-Path $consoleDest) {
    $consoleSize = (Get-ChildItem $consoleDest -Recurse | Measure-Object -Property Length -Sum).Sum
    Write-Host ("  of which console/: {0:N1} MB" -f ($consoleSize / 1MB))
}

function Measure-Startup([string]$label) {
    # --fake: a bare launch is a client of clockwork serve and starts one when none is
    # running (lab record, task 77), which on an instrument PC would take the boxes and
    # the console, and would outlive the Stop-Process below. The simulated window runs
    # the same imports and builds the same window, so the time is the same question.
    $proc = Start-Process -FilePath $exePath -ArgumentList "--fake" -PassThru
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
