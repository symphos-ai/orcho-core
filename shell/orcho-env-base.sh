#!/usr/bin/env bash
# shell/orcho-env-base.sh — Agnostic helpers for the Orcho pipeline engine.
#
# NOT sourced directly. Sourced from a workspace-specific orcho-env.sh:
#   source "$ORCH_CORE/shell/orcho-env-base.sh"

#
# Expects the calling script to have ALREADY exported:
#   ORCH_DIR  — absolute path to workspace-orchestrator
#   ORCH_CORE — absolute path to multiagent-core/
#
# After sourcing, exposes:
#   Variables: RUNS_DIR, PYTHON, ORCH_PY, CROSS_ORCH_PY
#   Functions: run_orch, run_cross_orch, orcho

# ---- Guard: ensure base variables are set ----------------------------------
if [[ -z "${ORCH_DIR:-}" || -z "${ORCH_CORE:-}" ]]; then
    echo "[orcho-env-base] ERROR: ORCH_DIR and ORCH_CORE must be set before sourcing orcho-env-base.sh" >&2
    return 1
fi

# ---- Standard paths --------------------------------------------------------
# Pipeline output lives in runspace/runs/{ts}/ — one atomic folder per run.
# RUNS_DIR is the parent; individual runs are created by the orchestrator.
RUNS_DIR="$ORCH_DIR/runspace/runs"


# Legacy module-file paths (kept for tooling that introspects them).
ORCH_PY="$ORCH_CORE/orchestrator.py"
CROSS_ORCH_PY="$ORCH_CORE/cross_orchestrator.py"

mkdir -p "$RUNS_DIR"

# ---- Activate .venv if present ---------------------------------------------
if [[ -f "$ORCH_CORE/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$ORCH_CORE/.venv/bin/activate"
    PYTHON="$ORCH_CORE/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

# ---- orcho (unified CLI facade) --------------------------------------------
# Single entry point for all pipeline commands.
# Usage: orcho run / orcho cross / orcho status / orcho metrics / orcho history
orcho() { (cd "$ORCH_CORE" && "$PYTHON" -m cli.orcho "$@"); }


# ---- run_orch --------------------------------------------------------------
run_orch() { (cd "$ORCH_CORE" && "$PYTHON" -m pipeline.orchestrator "$@"); }

# ---- run_cross_orch --------------------------------------------------------
run_cross_orch() { (cd "$ORCH_CORE" && "$PYTHON" -m pipeline.cross_orchestrator "$@"); }

# ---- log_phase (deprecated) ------------------------------------------------
# Pipeline now writes structured logs inside runs/{ts}/progress.log.
# Kept as a no-op shim so legacy workflow scripts don't break.
log_phase() {
    : # no-op: pipeline manages logs in runs/{ts}/ directly
}

# ---- archive_kanban (deprecated) -------------------------------------------
# Pipeline now uses runs/{ts}/ as an atomic run folder — no manual archiving needed.
# Kept as a no-op shim for backward compat.
archive_kanban() {
    local label="${1:-}"
    echo "[orcho-env] archive_kanban: no-op — pipeline uses runs/ directly (label=${label})"
}

log_phase "ENV" "orcho-env-base.sh loaded" "INIT"
