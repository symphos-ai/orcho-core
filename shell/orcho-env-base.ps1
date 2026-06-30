# shell/orcho-env-base.ps1
# Orcho — Multi-Agent Pipeline Engine
# Windows PowerShell environment setup (equivalent of orcho-env-base.sh)
#
# Usage:
#   . "$env:LOCALAPPDATA\orcho-core\shell\orcho-env-base.ps1"
#   # or add to $PROFILE for persistent setup

# ── 1. Stable engine (ORCHO_CORE) ────────────────────────────────────────────
if (-not $env:ORCHO_CORE) {
    $env:ORCHO_CORE = "$env:LOCALAPPDATA\orcho-core"
}

# ── 2. Dev engine (ORCHO_CORE_DEV) ───────────────────────────────────────────
if (-not $env:ORCHO_CORE_DEV) {
    $env:ORCHO_CORE_DEV = "$env:USERPROFILE\www\orcho"
}

# ── 3. orcho — runs from STABLE by default ───────────────────────────────────
function orcho {
    & "$env:ORCHO_CORE\.venv\Scripts\python.exe" -m cli.orcho @args
}

# ── 4. orcho-dev — runs from DEV copy ────────────────────────────────────────
function orcho-dev {
    & "$env:ORCHO_CORE_DEV\.venv\Scripts\python.exe" -m cli.orcho @args
}

# ── 5. Workspace switching ────────────────────────────────────────────────────
function Set-OrchoWorkspace-QCG {
    $env:ORCHO_WORKSPACE = "$env:USERPROFILE\www\qcg\workspace-orchestrator"
}
function Set-OrchoWorkspace-ATAS {
    $env:ORCHO_WORKSPACE = "$env:USERPROFILE\www\atas\workspace-orchestrator"
}

Set-Alias orcho-qcg  Set-OrchoWorkspace-QCG
Set-Alias orcho-atas Set-OrchoWorkspace-ATAS

# ── 6. Project shortcuts ──────────────────────────────────────────────────────
function orcho-unity { orcho run --project "$env:USERPROFILE\www\qcg\mag_unity_new-copy" @args }
function orcho-api   { orcho run --project "$env:USERPROFILE\www\qcg\magica_api_new" @args }
function orcho-stats { orcho run --project "$env:USERPROFILE\www\qcg\magica_stats" @args }

Write-Host "orcho env loaded: ORCHO_CORE=$env:ORCHO_CORE" -ForegroundColor Cyan
