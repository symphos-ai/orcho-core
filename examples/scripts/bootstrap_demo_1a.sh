#!/usr/bin/env bash
# bootstrap_demo_1a.sh — Prepare a disposable workspace + project copy
# for the DEMO-1A single-project CLI walkthrough.
#
# Why a copy: the mock pipeline writes inside the project tree. Pointing
# --project at a copy keeps the source fixture under examples/golden-api/
# untouched across re-runs. The copy is committed as a tiny git repo so the
# first run can exercise worktree isolation, review, and diff capture.
#
# Idempotent. Re-running wipes the previous demo dir before recreating
# it — but only if the dir carries our sentinel file, so a stray
# ORCHO_DEMO_ROOT pointing at live data refuses rather than rm -rf.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
core_dir="$(cd -- "$script_dir/../.." && pwd)"
fixture_src="$core_dir/examples/golden-api"

demo_root="${ORCHO_DEMO_ROOT:-/tmp/orcho_demo_1a}"
project_dir="$demo_root/project"
workspace_dir="$demo_root/workspace-orchestrator"
sentinel="$demo_root/.orcho-demo-1a"
printf -v project_arg "%q" "$project_dir"
printf -v workspace_arg "%q" "$workspace_dir"

run_workspace_init() {
  # Resolution order:
  #   1. explicit ORCHO_DEMO_ORCHO_BIN for installed CLI demos,
  #   2. explicit ORCHO_DEMO_CORE_PYTHON for source-checkout tests,
  #   3. repo venv for editable development,
  #   4. `orcho` on PATH (pipx / pip install),
  #   5. bare Python source fallback.
  local orcho_bin="${ORCHO_DEMO_ORCHO_BIN:-}"
  if [[ -n "$orcho_bin" ]]; then
    "$orcho_bin" workspace init "$demo_root" >/dev/null
    return
  fi

  local py_bin="${ORCHO_DEMO_CORE_PYTHON:-}"
  if [[ -n "$py_bin" ]]; then
    PYTHONPATH="$core_dir${PYTHONPATH:+:$PYTHONPATH}" \
      "$py_bin" -m cli.orcho workspace init "$demo_root" >/dev/null
    return
  fi

  py_bin="$core_dir/.venv/bin/python"
  if [[ -x "$py_bin" ]]; then
    PYTHONPATH="$core_dir${PYTHONPATH:+:$PYTHONPATH}" \
      "$py_bin" -m cli.orcho workspace init "$demo_root" >/dev/null
    return
  fi

  if command -v orcho >/dev/null 2>&1; then
    orcho workspace init "$demo_root" >/dev/null
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    py_bin="python3"
  else
    py_bin="python"
  fi
  PYTHONPATH="$core_dir${PYTHONPATH:+:$PYTHONPATH}" \
    "$py_bin" -m cli.orcho workspace init "$demo_root" >/dev/null
}

init_project_git() {
  (
    cd "$project_dir"
    git init -q
    git config user.email "demo@example.invalid"
    git config user.name "Orcho Demo"
    git add .
    git commit -q -m "Initial demo fixture"
  )
}

write_demo_plugin() {
  local plugin_dir="$project_dir/.orcho/multiagent"
  mkdir -p "$plugin_dir"
  cat >"$plugin_dir/plugin.py" <<'PY'
PLUGIN = {
    "name": "Golden API Demo",
    "language": "Python",
    "architecture": "Tiny validation module plus pytest tests",
    "file_hints": ["app/__init__.py", "app/validation.py", "tests/__init__.py"],
}
PY
}

# Defence in depth around the rm -rf below.
if [[ -z "$demo_root" || "$demo_root" == "/" ]]; then
  echo "ERROR: refusing to operate on demo_root='$demo_root'" >&2
  exit 1
fi
if [[ ! -d "$fixture_src" ]]; then
  echo "ERROR: source fixture not found: $fixture_src" >&2
  exit 1
fi

if [[ -e "$demo_root" ]]; then
  if [[ -f "$sentinel" ]]; then
    rm -rf "$demo_root"
  else
    echo "ERROR: $demo_root exists but is not an orcho-demo-1a directory." >&2
    echo "       Refusing to wipe. Remove it manually, or set" >&2
    echo "       ORCHO_DEMO_ROOT to a different path." >&2
    exit 1
  fi
fi

mkdir -p "$demo_root"
cp -R "$fixture_src" "$project_dir"
write_demo_plugin
init_project_git
run_workspace_init
touch "$sentinel"

cat <<EOF
DEMO-1A workspace ready.

  Project (copy):  $project_dir
  Workspace:       $workspace_dir
  Source fixture:  $fixture_src  (untouched)

Run the pipeline:

  orcho run \\
    --task "Fix validation bug in sample API" \\
    --project $project_arg \\
    --workspace $workspace_arg \\
    --profile advanced \\
    --mock \\
    --mock-validate-plan-reject 1 \\
    --max-rounds 2 \\
    --stream-output

Inspect the run:

  orcho evidence --format md --workspace $workspace_arg
  orcho status --workspace $workspace_arg
  orcho diff <run-id> --stat --workspace $workspace_arg
  orcho metrics --workspace $workspace_arg
EOF
