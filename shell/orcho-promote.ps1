# shell/orcho-promote.ps1
# Orcho — promote your DEV checkout of orcho-core to your STABLE install
# (Windows PowerShell equivalent of shell/orcho-promote).
#
# See docs/creator/09_dev_workflow.md for the full DEV <-> STABLE model.
#
# STABLE always tracks `main`; promoting from a feature branch is refused
# with a hint instead of merging behind your back. Each step is fail-fast:
# on error the script stops and later steps do not run.
#
# Paths resolve from the environment when set, with defaults that make the
# script work standalone:
#   ORCHO_CORE_DEV — your DEV checkout (defaults to the repo this script
#                    ships in, i.e. the parent of this shell\ directory)
#   ORCHO_CORE     — your STABLE install (defaults to
#                    $env:LOCALAPPDATA\orcho-core)
#
# Call it from anywhere by adding a function to your $PROFILE:
#   function orcho-promote { & "$env:ORCHO_CORE_DEV\shell\orcho-promote.ps1" @args }

$ErrorActionPreference = 'Stop'

$DevDefault = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$DevRepo = if ($env:ORCHO_CORE_DEV) { $env:ORCHO_CORE_DEV } else { $DevDefault }
$Stable  = if ($env:ORCHO_CORE)     { $env:ORCHO_CORE }     else { "$env:LOCALAPPDATA\orcho-core" }
$Python  = Join-Path $Stable '.venv\Scripts\python.exe'

function Fail($message) { Write-Host $message -ForegroundColor Red; exit 1 }

Write-Host "Promoting DEV -> STABLE (orcho-core)..." -ForegroundColor Cyan

# Step 0: branch guard. STABLE only ever tracks main; refuse a feature
# branch with a hint on how to merge it forward.
$branch = (git -C $DevRepo rev-parse --abbrev-ref HEAD)
if ($LASTEXITCODE -ne 0) { Fail "Cannot read current branch in $DevRepo" }
if ($branch -ne 'main') {
    Write-Host "DEV is on '$branch', not 'main'. STABLE only tracks main." -ForegroundColor Red
    Write-Host "  Merge into main first:"
    Write-Host "    git -C `"$DevRepo`" switch main; git -C `"$DevRepo`" merge --ff-only $branch; git -C `"$DevRepo`" push"
    Write-Host "  Then re-run orcho-promote."
    exit 1
}

Write-Host "[1/4] Push DEV (main) to GitHub..."
git -C $DevRepo push
if ($LASTEXITCODE -ne 0) { Fail "Push failed" }

Write-Host "[2/4] Pull into STABLE ($Stable)..."
git -C $Stable pull
if ($LASTEXITCODE -ne 0) { Fail "Pull failed" }

Write-Host "[3/4] Reinstall STABLE venv ($Stable\.venv)..."
# --force-reinstall so a version bump in pyproject.toml actually lands in
# importlib.metadata (plain `-e .` skips when sources are unchanged).
# No --no-deps: STABLE must receive orcho-core's normal runtime deps.
& $Python -m pip install --force-reinstall -e $Stable -q
if ($LASTEXITCODE -ne 0) { Fail "Install failed" }

Write-Host "[4/4] Installed package versions..."
$versions = @'
import importlib.metadata as md

for package in ("orcho-core", "tiktoken"):
    try:
        print(f"   {package}: {md.version(package)}")
    except md.PackageNotFoundError:
        print(f"   {package}: NOT INSTALLED")
'@
& $Python -c $versions

$head = (git -C $Stable log --oneline -1)
Write-Host "Done. STABLE is now at: $head" -ForegroundColor Green
