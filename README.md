# Orcho — Multi-Agent Pipeline Engine

[![PyPI](https://img.shields.io/pypi/v/orcho-core.svg)](https://pypi.org/project/orcho-core/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/orcho-core/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![CI](https://github.com/symphos-ai/orcho-core/actions/workflows/ci.yml/badge.svg)](https://github.com/symphos-ai/orcho-core/actions/workflows/ci.yml)
[![DCO](https://github.com/symphos-ai/orcho-core/actions/workflows/dco.yml/badge.svg)](https://github.com/symphos-ai/orcho-core/actions/workflows/dco.yml)
[![Release](https://github.com/symphos-ai/orcho-core/actions/workflows/release.yml/badge.svg)](https://github.com/symphos-ai/orcho-core/actions/workflows/release.yml)
[![codecov](https://codecov.io/gh/symphos-ai/orcho-core/branch/main/graph/badge.svg)](https://codecov.io/gh/symphos-ai/orcho-core)

**Orcho** — local-first control plane for agentic software delivery.
Use the coding agents you already trust; Orcho supervises the workflow
around them: plan → implementation → review → repair → final acceptance.

It is built for work that needs more structure than a single interactive
agent session:

- one task or one coordinated change across several repositories;
- explicit phase topology through profiles;
- human/agent review gates with resume and retry;
- durable run state: plans, diffs, findings, metrics, evidence;
- CLI, SDK, and MCP control surfaces.

Which model runs which phase is **fully configurable**.
Default: Claude (PLAN / BUILD / FIX) + Codex (REVIEW / QA).
Assign Claude, Codex, or Gemini to any phase via env vars, profiles,
or `config.local.json`.

Zero project-specific code — all project context comes through `plugin.py`.

---

## Try the golden mock demo

The fastest zero-API proof is the single-project CLI demo. It creates a
disposable git-backed fixture, runs the full mock pipeline, reviews the
diff, and writes evidence:

```bash
examples/scripts/bootstrap_demo_1a.sh
```

Then paste the printed `orcho run ... --mock` command and inspect:

```bash
orcho evidence --format md --workspace /tmp/orcho_demo_1a/workspace-orchestrator
orcho status --workspace /tmp/orcho_demo_1a/workspace-orchestrator
orcho diff <run-id> --stat --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

Full walkthrough: [docs/demos/demo-1a-single-project-cli.md](docs/demos/demo-1a-single-project-cli.md).

---

## First time? Start here

**→ [docs/user/00_getting_started.md](docs/user/00_getting_started.md)**

The full path from zero to the first result: prerequisites → install →
connect your project → first run.

---

## Install

**Recommended once the packages are published:**

```bash
pipx install orcho
orcho --help
```

Use `python -m pip install orcho` if you prefer a project-managed
environment over `pipx`.

Optional control surfaces are available through extras:

```bash
pipx install 'orcho[mcp]'
pipx install 'orcho[all]'
```

**Source checkout for development:**

```bash
git clone git@github.com:symphos-ai/orcho-core.git ~/.local/share/orcho-core
cd ~/.local/share/orcho-core
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```
Add to `~/.zshrc` / `~/.bashrc`:
```bash
export ORCHO_CORE="$HOME/.local/share/orcho-core"
orcho() { (source "$ORCHO_CORE/.venv/bin/activate" && "$ORCHO_CORE/.venv/bin/python" -m cli.orcho "$@"); }
```

**Windows (PowerShell):**

```powershell
git clone git@github.com:symphos-ai/orcho-core.git "$env:LOCALAPPDATA\orcho-core"
cd "$env:LOCALAPPDATA\orcho-core"
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
Add to `$PROFILE`:
```powershell
. "$env:LOCALAPPDATA\orcho-core\shell\orcho-env-base.ps1"
```

---

## How it works

```
Task
  → Claude  [PLAN]              writes the implementation plan
  → Codex   [validate_plan]     audits the plan
  → Claude  [BUILD]             implements the code
  → Codex   [REVIEW]            reviews the diff
  → Claude  [FIX]               fixes the findings
  → Codex   [final_acceptance]  final verdict
```

---

## Core commands

```bash
# One project
orcho run --task "Add input validation to /api/login" --project ~/my-project

# Several projects at once
orcho cross --task "Add rate limiting: API + client" \
            --projects api:~/api client:~/client

# No API calls (test)
orcho run --mock --task "..." --project ~/my-project

# Plan only (no code)
orcho run --profile planning --task "..." --project ~/my-project

# Resume an interrupted run
orcho run --resume 20260503_104135

# Status, history, metrics
orcho status | orcho history | orcho metrics
```

---

## Connecting a project

Create `your-project/.orcho/multiagent/plugin.py`:

```python
from pipeline.plugins import PluginConfig

plugin = PluginConfig(
    name="My Project",
    tech_stack="FastAPI + PostgreSQL",
    architecture="REST API. Routes: app/routes/, Services: app/services/",
    file_hints=["app/routes/", "app/services/", "tests/"],
    build_prompt_extra="Run: pytest -x after changes.",
    review_focus_extra="Check N+1 queries, missing validations.",
)
```

Without `plugin.py`, orcho runs in generic mode.

---

## Package layout

```
orcho-core/
├── cli/                            ← CLI facade (orcho run / cross / status…)
├── sdk/                            ← typed headless API for tools and embedders
├── pipeline/
│   ├── project_orchestrator.py     ← single-project pipeline
│   ├── cross_project/              ← cross-project planning, dispatch, gates
│   ├── runtime/                    ← profiles, steps, state, runner
│   ├── prompts/                    ← composable prompt parts and contracts
│   ├── control/                    ← handoff, resume, operator decisions
│   ├── engine/                     ← sessions, logging, worktrees, run diff
│   ├── evidence/                   ← evidence bundle and renderers
│   ├── profiles/                   ← profile loading and validation
│   ├── sandbox/                    ← command isolation backends
│   ├── skills/                     ← skill discovery and injection
│   ├── plugins.py                  ← PluginConfig + load_plugin()
│   └── checkpoint.py               ← SQLite store (--resume)
├── core/
│   ├── _prompts/                   ← core prompt templates
│   ├── _config/                    ← packaged defaults
│   ├── contracts/                  ← plan/review/release schemas
│   ├── infra/                      ← config, platform, binary discovery
│   ├── observability/              ← logging, metrics, trace
│   ├── io/                         ← retry, git helpers, prompt loader
│   └── context/                    ← codemap builder (optional)
├── agents/                         ← runtimes, registry, stream parsers
└── tests/                          ← unit, integration, acceptance, SDK contract tests
```

---

## Documentation

Ordered from general to specific — start at the top, go deeper as needed.

| Level | For whom | Link |
|---------|---------|--------|
| **User** | You want to use the system | [docs/user/](docs/user/) |
| **Expert** | You tune prompts, plugins, and models | [docs/expert/](docs/expert/) |
| **Integrator** | You author profiles, gates, and adapters | [docs/guides/](docs/guides/) |
| **Reference** | Exact schemas and registries | [docs/reference/](docs/reference/) |
| **Creator** | You develop the engine itself | [docs/creator/](docs/creator/) |

Full index: [docs/README.md](docs/README.md).

---

## Testing

```bash
pytest tests/ -q
pytest tests/unit/ -v
pytest tests/integration/ -v
```

Tests must not call real models. Use `MockAgentProvider` for
pipeline-flow scenarios.

---

## Key principles

- **Zero hardcoding** — all project context comes through `plugin.py`
- **DRY engine** — `pipeline/engine/` is shared by both orchestrators
- **3-level prompts** — project → workspace → core (always overridable)
- **Discoverable extension points** — `workspace init` creates safe
  `.orcho/` guides and templates without overwriting local edits
- **Resumable** — `--resume` continues from the last checkpoint
- **Cross-platform** — macOS, Linux, Windows (native + WSL2)
