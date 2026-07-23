#!/usr/bin/env bash
# bootstrap_demo_1b.sh — Prepare a disposable workspace + two-project copy
# for the DEMO-1B cross-project CLI walkthrough, and (re)start the
# API + Vite web watchers from the project copies.
#
# Companion to bootstrap_demo_1a.sh (single-project golden-api). The
# fixture under examples/cross-api-web/ ships a deliberate contract
# drift between two repos:
#
#   api/api/payload.py     emits  "email"
#   web/src/contracts.ts   sends/reads  "email_address"
#
# Each project's own local suite passes in isolation (API uses pytest,
# web uses node --test); the bug only surfaces when the two are looked
# at together. That's what orcho's cross-project pipeline +
# contract_check is for.
#
# Why a copy: the pipeline writes inside the project tree (modifies
# payload.py / TypeScript contract files, runs tests, etc.). Pointing
# --projects at copies keeps the source fixture under examples/cross-api-web/
# untouched across re-runs.
#
# What the script does on every run:
#   1. Empties ``$ORCHO_DEMO_ROOT`` and re-creates fresh ``api/`` +
#      ``web/`` copies + workspace dir. The default disposable root's
#      contents are always rebuilt from scratch; custom roots are
#      emptied only if their sentinel marker is present.
#   2. Kills any running demo API/web watchers on ``$ORCHO_DEMO_PORT``
#      (default 8000) and ``$ORCHO_DEMO_WEB_PORT`` (default 5173), then
#      starts fresh detached watchers. Logs live in
#      ``$ORCHO_DEMO_ROOT/server.log`` and ``$ORCHO_DEMO_ROOT/web.log``;
#      PIDs live in ``.server.pid`` and ``.web.pid``.
#   3. Prints the canonical orcho cross-project command + inspection
#      commands.
#
# Idempotent: re-running empties the previous demo dir before recreating
# it. A stray custom ``ORCHO_DEMO_ROOT`` pointing at live data refuses
# unless the dir carries the sentinel file.
set -euo pipefail

# ── Argument parsing ────────────────────────────────────────────────────────
#
# The demo can be staged in three additive phases. Default is full setup.
#
#   copy   Copy api/ + web/, init per-project git, start the demo server.
#          No workspace dir, no skills, no .mcp.json. The tail prints the
#          exact commands to run workspace init and wire MCP by hand.
#   init   copy + ``orcho workspace init`` (without ``--mcp-config``) +
#          seed demo skills. The tail prints how to wire MCP by hand.
#   mcp    init + write ``.mcp.json`` with the orcho-demo-1b server entry
#          (current default behaviour).

phase="mcp"
demo_language=""
demo_accounting_enabled=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    copy|init|mcp)
      phase="$1"
      shift
      ;;
    --language)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "ERROR: --language requires a non-empty value." >&2
        exit 2
      fi
      demo_language="$2"
      shift 2
      ;;
    --accounting|--enable-accounting)
      demo_accounting_enabled="true"
      shift
      ;;
    -h|--help|help)
      cat <<USAGE
Usage: $(basename "$0") [phase] [--language LANGUAGE] [--accounting]

Phases (additive — each includes the previous):
  copy   Copy projects only. Print manual workspace-init + MCP instructions.
  init   copy + run 'orcho workspace init' without --mcp-config + seed skills.
         Print manual MCP-wiring instructions.
  mcp    Full setup including .mcp.json (default).

Options:
  --language LANGUAGE   Write language.plan_language and language.task_language
                        into workspace-orchestrator/.orcho/config.local.json.
  --accounting          Write accounting.enabled=true into config.local.json.

Environment:
  ORCHO_DEMO_BASE_DIR   Parent of the disposable demo root.
                        Default: $HOME/www
  ORCHO_DEMO_ROOT       Override the demo root path entirely.
  ORCHO_DEMO_PORT       Demo API port (default 8000).
  ORCHO_DEMO_WEB_PORT   Vite web dev port (default 5173).
  ORCHO_DEMO_NPM        npm binary (default 'npm').
  ORCHO_DEMO_PYTHON     python binary for the demo server (default python3).
  ORCHO_DEMO_CORE_PYTHON
                        python binary for the orcho workspace-init call
                        (default: repo .venv, then python3).
  ORCHO_DEMO_MCP_COMMAND
                        Override the orcho-mcp command written into
                        .mcp.json / printed in manual snippets. By
                        default the script prefers the STABLE install
                        at ~/.local/share/orcho-core/.venv/bin/orcho-mcp,
                        falling back to 'orcho-mcp' on PATH.
USAGE
      exit 0
      ;;
    *)
      if [[ "$1" == -* ]]; then
        echo "ERROR: unknown argument '$1'." >&2
      else
        echo "ERROR: unknown phase '$1'. Use one of: copy, init, mcp (default: mcp)." >&2
      fi
      echo "       Run '$(basename "$0") --help' for details." >&2
      exit 2
      ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
core_dir="$(cd -- "$script_dir/../.." && pwd)"
fixture_src="$core_dir/examples/cross-api-web"

demo_base_dir="${ORCHO_DEMO_BASE_DIR:-$HOME/www}"
default_demo_root="$demo_base_dir/orcho-demo/orcho_demo_1b"
demo_root="${ORCHO_DEMO_ROOT:-$default_demo_root}"
case "$demo_root" in
  /*) ;;
  *) demo_root="${PWD%/}/$demo_root" ;;
esac
demo_root="${demo_root%/}"
demo_port="${ORCHO_DEMO_PORT:-8000}"
web_port="${ORCHO_DEMO_WEB_PORT:-5173}"
api_project="$demo_root/api"
web_project="$demo_root/web"
workspace_dir="$demo_root/workspace-orchestrator"
db_path="$demo_root/demo.db"
server_log="$demo_root/server.log"
server_pid_file="$demo_root/.server.pid"
web_log="$demo_root/web.log"
web_pid_file="$demo_root/.web.pid"
sentinel="$demo_root/.orcho-demo-1b"
mcp_config_file="$demo_root/.mcp.json"
demo_server_src="$fixture_src/api/demo_server.py"
demo_server="$api_project/demo_server.py"

printf -v api_arg "%q" "$api_project"
printf -v web_arg "%q" "$web_project"
printf -v workspace_arg "%q" "$workspace_dir"

resolve_mcp_command() {
  # The demo is meant to drive the user's installed Orcho, not the source
  # checkout's dev venv. Resolution order:
  #
  #   1. ``ORCHO_DEMO_MCP_COMMAND`` — explicit override always wins.
  #   2. STABLE install — ``~/.local/share/orcho-core/.venv/bin/orcho-mcp``,
  #      produced by ``orcho-promote``.
  #   3. ``orcho-mcp`` on PATH — resolved to an absolute path so the
  #      generated ``.mcp.json`` survives MCP hosts that don't share
  #      the demo shell's PATH.
  #   4. Literal ``orcho-mcp`` — MCP host resolves at launch time.
  #
  # The source checkout's ``.venv`` is deliberately NOT a candidate: a
  # demo that silently runs dev code is a footgun.

  if [[ -n "${ORCHO_DEMO_MCP_COMMAND:-}" ]]; then
    echo "$ORCHO_DEMO_MCP_COMMAND"
    return
  fi

  local stable="$HOME/.local/share/orcho-core/.venv/bin/orcho-mcp"
  if [[ -x "$stable" ]]; then
    echo "$stable"
    return
  fi

  local on_path
  if on_path="$(command -v orcho-mcp 2>/dev/null)" && [[ -n "$on_path" ]]; then
    echo "$on_path"
    return
  fi

  echo "orcho-mcp"
}

run_workspace_init() {
  # ``wire_mcp=1`` writes ``.mcp.json``; ``0`` only creates the workspace
  # layout and prints nothing about MCP. Both modes pass ``--force``
  # because the demo root is disposable.
  local wire_mcp="${1:-1}"

  # Interpreter resolution: an explicit ORCHO_DEMO_CORE_PYTHON wins (lets
  # tests and callers pin the interpreter that actually has orcho's
  # dependencies), then the repo venv, then bare python3. The bare fallback
  # requires a python3 new enough for this package — a stale system python3
  # (e.g. macOS/Xcode 3.9) fails at import time. Distinct from
  # ORCHO_DEMO_PYTHON, which drives only the demo API server.
  local py_bin="${ORCHO_DEMO_CORE_PYTHON:-}"
  if [[ -z "$py_bin" ]]; then
    py_bin="$core_dir/.venv/bin/python"
    if [[ ! -x "$py_bin" ]]; then
      if command -v python3 >/dev/null 2>&1; then
        py_bin="python3"
      else
        py_bin="python"
      fi
    fi
  fi

  local mcp_args=()
  if [[ "$wire_mcp" == "1" ]]; then
    mcp_args=(
      --mcp-config "$mcp_config_file"
      --mcp-server-name "orcho-demo-1b"
      --orcho-mcp-command "$(resolve_mcp_command)"
    )
  fi

  # ``${arr[@]+"${arr[@]}"}`` keeps the empty-array expansion safe under
  # ``set -u`` on bash 3.2 (still shipped as /bin/bash on macOS).
  PYTHONPATH="$core_dir${PYTHONPATH:+:$PYTHONPATH}" \
    "$py_bin" -m cli.orcho workspace init "$demo_root" \
      ${mcp_args[@]+"${mcp_args[@]}"} \
      --force \
      >/dev/null

  if [[ -z "$demo_language" && -z "$demo_accounting_enabled" ]]; then
    return
  fi

  "$py_bin" - \
    "$workspace_dir/.orcho/config.local.json" \
    "$demo_language" \
    "$demo_accounting_enabled" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
language = sys.argv[2].strip()
accounting_enabled = bool(sys.argv[3])
data = json.loads(path.read_text(encoding="utf-8"))

if language:
    language_config = data.get("language")
    if not isinstance(language_config, dict):
        language_config = {}
        data["language"] = language_config
    language_config["plan_language"] = language
    language_config["task_language"] = language

if accounting_enabled:
    accounting_config = data.get("accounting")
    if not isinstance(accounting_config, dict):
        accounting_config = {}
        data["accounting"] = accounting_config
    accounting_config["enabled"] = True

path.write_text(
    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY
}

write_demo_skill() {
  local skill_dir="$1"
  local name="$2"
  local description="$3"
  local body="$4"

  mkdir -p "$skill_dir"
  cat > "$skill_dir/SKILL.md" <<EOF_SKILL
---
name: $name
description: $description
---

$body
EOF_SKILL
}

link_client_skill_mirrors() {
  local root_dir="$1"

  mkdir -p "$root_dir/.claude"
  rm -rf "$root_dir/.claude/skills"
  ln -s "../.agents/skills" "$root_dir/.claude/skills"

  mkdir -p "$root_dir/.agents"
  mkdir -p "$root_dir/.agents/skills"
}

seed_workspace_skills() {
  local skills_root="$workspace_dir/.agents/skills"
  mkdir -p "$skills_root"

  write_demo_skill \
    "$skills_root/team-lead" \
    "team-lead" \
    "Use for product-level planning, cross-project API/frontend coordination, task decomposition, contract mismatch triage, phase routing, and final evidence summaries." \
    $'## Product map\n\nThe demo product is a tiny admin tool with a Python API producer and a Vue.js/TypeScript frontend consumer. The API exposes users, teams, and projects. The frontend submits forms to `/api/{users,teams,projects}` and renders the returned rows.\n\n## Phase usage\n\n- DECOMPOSE / PLAN: identify whether the task belongs to API, frontend, QA, or more than one project. Assign exact skill names in subtasks.\n- IMPLEMENT / FIX: route API payload work to `backend-python` and Vue contract work to `frontend-vuejs`.\n- REVIEW / FINAL_QA: route backend tests to `backend-qa`, frontend tests to `frontend-qa`, then summarize cross-project evidence.\n\n## Routing rules\n\n- If the user asks for a plan, first explain the affected product surface.\n- For payload mismatches, compare the API producer with the frontend field contract before choosing the implementation owner.\n- Keep API and frontend changes in separate repo histories.\n- Evidence must name the exact commands run in each project.'

  link_client_skill_mirrors "$workspace_dir"
}

write_project_plugin() {
  local project_dir="$1"
  mkdir -p "$project_dir/.orcho/multiagent"
  cat > "$project_dir/.orcho/multiagent/plugin.py" <<'EOF_PLUGIN'
from pipeline.skills import SkillTrustPolicy

PLUGIN = {
    "skill_trust": SkillTrustPolicy(trust_project=True),
}
EOF_PLUGIN
}

seed_project_skills() {
  local project_dir="$1"
  local project_kind="$2"
  local skills_root="$project_dir/.agents/skills"
  mkdir -p "$skills_root"

  write_project_plugin "$project_dir"

  if [[ "$project_kind" == "api" ]]; then
    write_demo_skill \
      "$skills_root/backend-python" \
      "backend-python" \
      "Use for professional Python backend implementation in the demo API project: payload producers, route modules, SQLite writes, response contracts, and API-side bug fixes." \
      $'## Scope\n\nFor Python API subtasks, first locate the payload producers, route handlers, persistence writes, and backend tests relevant to the current subtask.\n\n## Workflow\n\n1. Read the existing producer shape before editing.\n2. Keep changes surgical and preserve unrelated route behavior.\n3. Make emitted JSON field names match the agreed frontend contract.\n4. Prefer simple Python and explicit tests over broad abstractions.\n\n## Verification\n\nRun the API project pytest suite, or report the exact blocker.'

    write_demo_skill \
      "$skills_root/backend-qa" \
      "backend-qa" \
      "Use for backend unit tests, API response contract assertions, pytest coverage, regression checks, and review-phase verification in the demo API project." \
      $'## Scope\n\nYou own backend verification. Focus on pytest tests for API payload fields, route behavior, and regression coverage.\n\n## Phase usage\n\n- REVIEW: inspect backend diff and identify missing assertions.\n- FIX: add or update targeted pytest coverage.\n- FINAL_QA: report exact backend commands and outcomes.\n\n## Rules\n\n- Test the public payload shape, not private implementation details.\n- Include the failing contract field when the task is about API/frontend drift.\n- Keep tests deterministic and local to the API project.'
  else
    write_demo_skill \
      "$skills_root/frontend-vuejs" \
      "frontend-vuejs" \
      "Use for professional Vue.js and TypeScript frontend implementation in the demo web project: forms, REST consumers, contract fields, rendering, and UI behavior." \
      $'## Scope\n\nFor Vue.js/TypeScript frontend subtasks, first locate the consumer contract, form/section configuration, app entry point, and browser shell relevant to the current subtask.\n\n## Workflow\n\n1. Trace form fields from their source configuration into the REST request body.\n2. Trace response fields through the consumer contract before rendering changes.\n3. Keep browser-served TypeScript compatible with the demo static server.\n4. Preserve unrelated admin UI behavior.\n\n## Verification\n\nRun the frontend node tests, or report the exact blocker.'

    write_demo_skill \
      "$skills_root/frontend-qa" \
      "frontend-qa" \
      "Use for frontend unit tests, Vue/TypeScript contract assertions, browser-facing regression checks, and review-phase verification in the demo web project." \
      $'## Scope\n\nYou own frontend verification. Focus on node tests that prove contract constants, section bindings, and form-to-payload behavior.\n\n## Phase usage\n\n- REVIEW: inspect frontend diff and identify missing consumer assertions.\n- FIX: add or update targeted node tests.\n- FINAL_QA: report exact frontend commands and outcomes.\n\n## Rules\n\n- Assert the consumer contract and form/section bindings at their source.\n- Keep tests independent from the Python API server unless the task asks for an end-to-end smoke.\n- Make failures point to the contract field or UI binding that drifted.'
  fi

  link_client_skill_mirrors "$project_dir"
}

prepare_web_dependencies() {
  local npm_bin="${ORCHO_DEMO_NPM:-npm}"
  if ! command -v "$npm_bin" >/dev/null 2>&1; then
    echo "ERROR: npm is required to prepare the demo web project." >&2
    echo "       Install Node.js/npm, or set ORCHO_DEMO_NPM to an npm-compatible binary." >&2
    exit 1
  fi

  (
    cd "$web_project"
    echo "Installing demo web dependencies in $web_project ..."
    if [[ -f package-lock.json ]]; then
      "$npm_bin" ci --no-audit --no-fund
    else
      "$npm_bin" install --no-audit --no-fund
    fi
  )

  if [[ ! -x "$web_project/node_modules/.bin/vue-tsc" ]]; then
    echo "ERROR: web dependencies were installed, but vue-tsc is still unavailable." >&2
    echo "       npm run build would fail in $web_project." >&2
    exit 1
  fi
}

# Defence in depth around the rm -rf below.
if [[ -z "$demo_root" || "$demo_root" == "/" ]]; then
  echo "ERROR: refusing to operate on demo_root='$demo_root'" >&2
  exit 1
fi
if [[ ! -d "$fixture_src/api" || ! -d "$fixture_src/web" ]]; then
  echo "ERROR: source fixture not found: $fixture_src (needs api/ and web/)" >&2
  exit 1
fi
if [[ ! -f "$demo_server_src" ]]; then
  echo "ERROR: demo_server.py not found: $demo_server_src" >&2
  exit 1
fi

# The script may be launched from inside a disposable subdir. Move to
# stable source checkout before cleaning the tree, otherwise later
# subprocesses can inherit a removed cwd and print getcwd/chdir warnings.
cd "$core_dir"

# ── 1. Kill previous demo watchers (if any) ──────────────────────────────────
#
# Two paths: the recorded PID files from a previous bootstrap, and a
# fallback that scans the port. The recorded PID is the safer signal —
# the port scan kills whatever happens to be bound there, which could
# be something unrelated.

kill_pid_file() {
  local pid_file="$1"
  local old_pid
  if [[ ! -f "$pid_file" ]]; then
    return
  fi
  old_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" 2>/dev/null || true
    # Give it a moment to release the socket cleanly.
    for _ in 1 2 3 4 5; do
      kill -0 "$old_pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$old_pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

kill_port_listeners() {
  local port="$1"
  local stragglers
  if ! command -v lsof >/dev/null 2>&1; then
    return
  fi
  stragglers="$(lsof -ti TCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$stragglers" ]]; then
    echo "Killing existing listener(s) on port $port: $stragglers"
    # shellcheck disable=SC2086
    kill -9 $stragglers 2>/dev/null || true
  fi
}

kill_pid_file "$server_pid_file"
kill_pid_file "$web_pid_file"

# Fallback: anything else still bound to the port (TIME_WAIT-friendly
# via ``allow_reuse_address`` on the server, but a live listener
# would still block).
kill_port_listeners "$demo_port"
kill_port_listeners "$web_port"

# ── 2. Wipe + recreate demo dirs ─────────────────────────────────────────────

empty_dir() {
  local dir="$1"
  if [[ -d "$dir" && ! -L "$dir" ]]; then
    find "$dir" -mindepth 1 -exec rm -rf {} +
  else
    rm -rf "$dir"
    mkdir -p "$dir"
  fi
}

if [[ -e "$demo_root" ]]; then
  if [[ ! -d "$demo_root" ]]; then
    echo "ERROR: $demo_root exists but is not a directory." >&2
    echo "       Remove it manually, or set ORCHO_DEMO_ROOT elsewhere." >&2
    exit 1
  fi

  if [[ "$demo_root" == "$default_demo_root" || -f "$sentinel" ]]; then
    # Preserve the top-level project directory inodes when they already
    # exist. If someone reruns this script from inside api/ or web/,
    # deleting those directories would strand their parent shell in the
    # old removed repo; clearing contents keeps `git status` pointed at
    # the freshly committed demo project.
    empty_dir "$api_project"
    empty_dir "$web_project"
    empty_dir "$workspace_dir"
    find "$demo_root" -mindepth 1 -maxdepth 1 \
      ! -name api ! -name web ! -name workspace-orchestrator \
      -exec rm -rf {} +
  else
    echo "ERROR: $demo_root exists but is not an orcho-demo-1b directory." >&2
    echo "       Refusing to empty it. Remove it manually, or set" >&2
    echo "       ORCHO_DEMO_ROOT to a different path." >&2
    exit 1
  fi
fi

mkdir -p "$api_project" "$web_project" "$demo_root"
# ``empty_dir`` above creates ``workspace-orchestrator/`` even on a fresh
# rerun. In ``copy`` mode we don't want that dead dir lying around.
if [[ "$phase" == "copy" && -d "$workspace_dir" ]]; then
  rmdir "$workspace_dir" 2>/dev/null || true
fi
cp -R "$fixture_src/api/." "$api_project"
cp -R "$fixture_src/web/." "$web_project"
cp "$fixture_src/Makefile" "$demo_root/Makefile"
prepare_web_dependencies

# Phase gating: workspace dir + skills land at ``init`` and above; the
# ``.mcp.json`` lands only at ``mcp``. ``copy`` is the bare-fixture mode.
case "$phase" in
  init)
    run_workspace_init 0
    seed_workspace_skills
    seed_project_skills "$api_project" "api"
    seed_project_skills "$web_project" "web"
    ;;
  mcp)
    run_workspace_init 1
    seed_workspace_skills
    seed_project_skills "$api_project" "api"
    seed_project_skills "$web_project" "web"
    ;;
  copy)
    : # bare copy + git + server only
    ;;
esac
touch "$sentinel"

# Each project becomes its own git repo — the contract_check phase
# reads the diff per project, so the two trees must have independent
# git state. ``user.email`` / ``user.name`` set locally so the commit
# works even on machines without a global git identity.
#
# The demo root itself is deliberately NOT a git repo: api/ and web/
# are the only meaningful sources of truth, and a wrapping repo would
# only confuse "where do I run git status" during the demo. Run git
# from inside api/ or web/.
for repo in "$api_project" "$web_project"; do
  (
    cd "$repo"
    git init -q
    git config user.email "demo@orcho.local"
    git config user.name "Orcho Demo"
    git add .
    git commit -qm "init"
  )
done

# ── 3. Start the demo API + web watchers (detached) ─────────────────────────

# ``nohup`` + ``&`` so the server outlives this script. ``setsid`` is
# Linux-only and macOS does not ship it by default; nohup alone is
# enough for the demo. Pick whichever python is on PATH — sticks with
# the user's shell defaults; the API server is stdlib-only so any
# 3.10+ python works.

python_bin="${ORCHO_DEMO_PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
  python_bin="python"
fi
npm_bin="${ORCHO_DEMO_NPM:-npm}"

nohup "$python_bin" "$demo_server" \
  --api "$api_project" \
  --web "$web_project" \
  --db  "$db_path" \
  --port "$demo_port" \
  > "$server_log" 2>&1 &
server_pid=$!
echo "$server_pid" > "$server_pid_file"
disown "$server_pid" 2>/dev/null || true

# Wait until the server is actually accepting on the port (up to ~5s)
# so the printed instructions can be acted on immediately.
api_base_url="http://localhost:$demo_port/api"
api_health_url="$api_base_url/meta"
web_url="http://localhost:$web_port/"
for _ in $(seq 1 25); do
  if curl -s -o /dev/null -w "%{http_code}" "$api_health_url" 2>/dev/null \
     | grep -qE '^(200|3[0-9]{2})$'; then
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "ERROR: demo server failed to start. Log tail:" >&2
    tail -n 30 "$server_log" >&2 || true
    exit 1
  fi
  sleep 0.2
done

(
  cd "$web_project"
  exec env \
    ORCHO_DEMO_PORT="$demo_port" \
    ORCHO_DEMO_WEB_PORT="$web_port" \
    "$npm_bin" run dev -- --host 127.0.0.1 --port "$web_port"
) > "$web_log" 2>&1 &
web_pid=$!
echo "$web_pid" > "$web_pid_file"
disown "$web_pid" 2>/dev/null || true

for _ in $(seq 1 50); do
  if curl -s -o /dev/null -w "%{http_code}" "$web_url" 2>/dev/null \
     | grep -qE '^(200|3[0-9]{2})$'; then
    break
  fi
  if ! kill -0 "$web_pid" 2>/dev/null; then
    echo "ERROR: Vite web watcher failed to start. Log tail:" >&2
    tail -n 30 "$web_log" >&2 || true
    exit 1
  fi
  sleep 0.2
done

mcp_cmd="$(resolve_mcp_command)"
printf -v mcp_cmd_arg "%q" "$mcp_cmd"
printf -v mcp_config_arg "%q" "$mcp_config_file"
printf -v demo_root_arg "%q" "$demo_root"

cat <<EOF
DEMO-1B workspace ready  (phase: $phase)

  API project (copy):  $api_project
  WEB project (copy):  $web_project
  Source fixture:      $fixture_src  (untouched)
  SQLite db:           $db_path  (3 users, 2 teams, 2 projects seeded)

  API watcher (re)started from the API repo:
    API:  $api_base_url
    PID:  $server_pid  (file: $server_pid_file)
    Log:  $server_log
    File: $demo_server

  Web watcher (Vite + HMR, proxies /api to :$demo_port):
    URL:  $web_url  (proxies /api to :$demo_port)
    PID:  $web_pid  (file: $web_pid_file)
    Log:  $web_log

Open $web_url in a browser.
Submit /users/new → HTTP 500.
Submit /teams/new or /projects/new → 201, row appears in the list.

Local config options for another run:

  $(basename "$0") $phase --language French --accounting
  $(basename "$0") $phase --language English

  --language writes language.plan_language + language.task_language.
  --accounting writes accounting.enabled=true; omit it to keep accounting off.

EOF

# Phase-specific tail: workspace + MCP layout, and how to run the pipeline.
case "$phase" in
  copy)
    cat <<EOF
Workspace + MCP NOT configured (phase=copy).

To finish setup by hand:

  # 1. Create the workspace layout and write the MCP config in one go:
  orcho workspace init $demo_root_arg \\
    --mcp-config $mcp_config_arg \\
    --mcp-server-name orcho-demo-1b \\
    --orcho-mcp-command $mcp_cmd_arg \\
    --force

  # Or split into two passes:
  #   orcho workspace init $demo_root_arg --force           # layout only
  #   orcho workspace init $demo_root_arg --mcp-config $mcp_config_arg \\
  #       --mcp-server-name orcho-demo-1b \\
  #       --orcho-mcp-command $mcp_cmd_arg --force          # add .mcp.json

  # 2. Re-run this script with 'init' or 'mcp' phase to also seed demo
  #    skills under workspace-orchestrator/.agents/skills and per-project
  #    .agents/skills, or seed them by hand.

Then jump to the 'orcho cross' invocation shown by the 'mcp' phase.

Stop demo watchers later with:
  kill \$(cat $server_pid_file) \$(cat $web_pid_file)
EOF
    ;;

  init)
    printf -v workspace_arg_local "%q" "$workspace_dir"
    cat <<EOF
Workspace created; MCP NOT wired (phase=init).

  Workspace:           $workspace_dir
  Local config:        $workspace_dir/.orcho/config.local.json
  Demo skills:         $workspace_dir/.agents/skills
                       $api_project/.agents/skills
                       $web_project/.agents/skills
  Client mirrors:      $workspace_dir/.claude/skills -> ../.agents/skills
                       $api_project/.claude/skills -> ../.agents/skills
                       $web_project/.claude/skills -> ../.agents/skills
  Skill roster:
    workspace: team-lead
    api:       backend-python, backend-qa
    web:       frontend-vuejs, frontend-qa

To wire MCP by hand, write $mcp_config_file with:

  {
    "mcpServers": {
      "orcho-demo-1b": {
        "command": "$mcp_cmd",
        "args": [],
        "env": {
          "ORCHO_WORKSPACE": "$workspace_dir"
        }
      }
    }
  }

Or re-run this script with the 'mcp' phase to generate it.

Run the cross-project pipeline (real LLM, ~\$0.25, ~3 min):

  orcho cross \\
    --projects api:$api_arg web:$web_arg \\
    --task "Align the user payload contract between api and web — the field name for user email must be consistent across both projects." \\
    --workspace $workspace_arg_local \\
    --max-rounds 2 \\
    --output live

Inspect the run:

  orcho status   --workspace $workspace_arg_local
  orcho metrics  --workspace $workspace_arg_local
  orcho evidence --format md --workspace $workspace_arg_local

Run the per-project test suites (each passes in isolation; mismatch only
shows up cross-project):

  make -C $demo_root_arg test-api      # cd api && python -m pytest -q
  make -C $demo_root_arg test-web      # cd web && node --test tests/contracts.test.mjs && npm run build
  make -C $demo_root_arg test          # both

Stop demo watchers later with:
  kill \$(cat $server_pid_file) \$(cat $web_pid_file)
EOF
    ;;

  mcp)
    cat <<EOF
Workspace + MCP fully configured (phase=mcp).

  Workspace:           $workspace_dir
  MCP config:          $mcp_config_file  (server name: orcho-demo-1b)
  Local config:        $workspace_dir/.orcho/config.local.json
  Demo skills:         $workspace_dir/.agents/skills
                       $api_project/.agents/skills
                       $web_project/.agents/skills
  Client mirrors:      $workspace_dir/.claude/skills -> ../.agents/skills
                       $api_project/.claude/skills -> ../.agents/skills
                       $web_project/.claude/skills -> ../.agents/skills
  Skill roster:
    workspace: team-lead
    api:       backend-python, backend-qa
    web:       frontend-vuejs, frontend-qa
  Phase routing UX:
    DECOMPOSE/PLAN: team-lead maps product flow and assigns exact skills
    IMPLEMENT/FIX:  backend-python + frontend-vuejs own code changes
    REVIEW/QA:      backend-qa + frontend-qa own test and evidence gaps
    FINAL_QA:       team-lead summarizes cross-project outcome

Run the cross-project pipeline (real LLM, ~\$0.25, ~3 min):

  orcho cross \\
    --projects api:$api_arg web:$web_arg \\
    --task "Align the user payload contract between api and web — the field name for user email must be consistent across both projects." \\
    --workspace $workspace_arg \\
    --max-rounds 2 \\
    --output live

After it lands, refresh the browser and submit /users/new again — the
server hot-reloads the API producer module, and the Vue frontend reads
the fixed REST response, so the same form now returns 201 and the new
user appears in the SQLite table.

Inspect the run:

  orcho status   --workspace $workspace_arg
  orcho metrics  --workspace $workspace_arg
  orcho evidence --format md --workspace $workspace_arg

Run the per-project test suites (each passes in isolation; mismatch only
shows up cross-project):

  make -C $demo_root_arg test-api      # cd api && python -m pytest -q
  make -C $demo_root_arg test-web      # cd web && node --test tests/contracts.test.mjs && npm run build
  make -C $demo_root_arg test          # both

Stop demo watchers later with:
  kill \$(cat $server_pid_file) \$(cat $web_pid_file)
EOF
    ;;
esac
