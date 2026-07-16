# Orcho — Production Harness for Agentic Software Delivery

[![PyPI](https://img.shields.io/pypi/v/orcho-core.svg)](https://pypi.org/project/orcho-core/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/orcho-core/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![CI](https://github.com/symphos-ai/orcho-core/actions/workflows/ci.yml/badge.svg)](https://github.com/symphos-ai/orcho-core/actions/workflows/ci.yml)
[![DCO](https://github.com/symphos-ai/orcho-core/actions/workflows/dco.yml/badge.svg)](https://github.com/symphos-ai/orcho-core/actions/workflows/dco.yml)
[![Release](https://github.com/symphos-ai/orcho-core/actions/workflows/release.yml/badge.svg)](https://github.com/symphos-ai/orcho-core/actions/workflows/release.yml)
[![codecov](https://codecov.io/gh/symphos-ai/orcho-core/branch/main/graph/badge.svg)](https://codecov.io/gh/symphos-ai/orcho-core)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/symphos-ai/orcho-core/badge)](https://scorecard.dev/viewer/?uri=github.com/symphos-ai/orcho-core)

**Orcho** is a production harness and control plane for agentic software
delivery.

**Run one task. Watch Orcho plan, implement, reject false-ready work, repair
it, and prove what is ready to deliver.**

📖 **Documentation:** [docs.orcho.dev](https://docs.orcho.dev)

![One orcho run end to end, sped up: the opening envelope, the pipeline map, the plan contract, plan validation, implement subtasks with attestations, review, final acceptance, the delivery commit, and the closing rollup](https://raw.githubusercontent.com/symphos-ai/orcho-core/main/docs/assets/orcho-run-demo.gif)

<sub>One `orcho run` end to end (mock pipeline, sped up). Interactive version
with pause and scrub: [docs.orcho.dev](https://docs.orcho.dev).</sub>

Use the coding agents you already trust. They remain the workers; Orcho owns
the delivery protocol around them: plan → implementation → review → repair
→ final acceptance.

It is built for work that needs more structure than a single interactive
agent session:

- one task or one coordinated change across several repositories;
- explicit phase topology through profiles;
- human/agent review gates with resume and retry;
- durable run state: plans, diffs, findings, metrics, evidence;
- CLI, SDK, and MCP control surfaces.

Which model runs which phase is **fully configurable**.
Default: Claude (PLAN / BUILD / FIX) + Codex (REVIEW / QA).
Assign registered runtimes such as Claude, a Claude-compatible GLM wrapper,
Codex, or Gemini to any phase via env vars, profiles, or `config.local.json`.

No engine fork is required for project-specific context. Orcho can run in
generic mode; add an optional `plugin.py` when the project needs explicit
architecture, file hints, prompts, or verification policy.

---

## Install

`orcho` is the native CLI distribution — it installs the core CLI **and** the
MCP server (`orcho-mcp`). The recommended path is `pipx`, which keeps the CLI
isolated from any project environment. Pick your OS below, or jump to the
OS-agnostic [Docker](#docker) / [direct engine](#direct-engine-dependency)
paths.

Prerequisites on every OS: **Python 3.12+**, and for real (non-`--mock`) runs at
least one code-agent CLI or compatible wrapper (`claude`, `claude-glm`,
`codex`, or `gemini`) available to Orcho.

> `pipx ensurepath` updates `PATH` for **future** shells, not the one you run it
> in. So after `ensurepath` you must **open a new terminal** before `pipx` (and
> the installed `orcho`) are on `PATH` — this trips up first-time Windows setups
> in particular. Each block below marks exactly where to reopen the shell.

### macOS

```bash
brew install pipx        # skip if pipx is already installed
pipx ensurepath
# ↻ reopen your terminal so the installed `orcho` is on PATH:
pipx install orcho
orcho --help
```

### Linux

```bash
python3 -m pip install --user pipx   # or: sudo apt install pipx / sudo dnf install pipx
python3 -m pipx ensurepath
# ↻ reopen your terminal so `pipx` (and later `orcho`) are on PATH:
pipx install orcho
orcho --help
```

### Windows

Native Windows is supported and exercised in CI. Install
[Python 3.12+](https://python.org) and [Git for Windows](https://git-scm.com/download/win)
first, then, in **PowerShell**:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# ↻ IMPORTANT: close this window and open a NEW PowerShell now — `ensurepath`
#   only updates PATH for new shells, so `pipx` is not found until you reopen.
pipx install orcho
orcho --help
```

Prefer a Unix shell? Install into **WSL2** using the Linux steps above. Full
Windows notes — agent-CLI paths, WSL2 layout, and pipe-based output streaming —
are in [docs/expert/05_windows.md](docs/expert/05_windows.md).

### Docker

OS-agnostic. Use Docker to try Orcho without installing its Python package or
agent CLIs on the host:

```bash
docker pull ghcr.io/symphos-ai/orcho
alias orcho='docker run --rm -it \
  -v "$PWD":/workspace \
  -v ~/.orcho-auth:/agent-auth:ro \
  ghcr.io/symphos-ai/orcho orcho'

orcho run --project /workspace --task "Add input validation to the login endpoint."
```

The image includes the core CLI and MCP server. See
[`orcho` Docker docs](https://github.com/symphos-ai/orcho/tree/main/docker)
for credential bootstrap, MCP stdio setup, and custom project toolchains.

### Direct engine dependency

OS-agnostic. Use `pip` when you intentionally want `orcho-core` in the active
virtualenv, CI image, devcontainer, or custom image:

```bash
python -m pip install orcho-core
```

The `orcho` distribution depends on `orcho-core`; most CLI users should start
with `orcho`, while integrators can depend on `orcho-core` directly. The
`orcho[mcp]`/`orcho[all]` extras remain as no-op back-compat aliases.

For source-checkout setup, tests, and contribution workflow, see
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Try the golden mock demo

The fastest zero-API proof is the single-project CLI demo. It creates a
disposable git-backed fixture, runs the full mock pipeline, reviews the diff,
and writes evidence.

For an installed CLI, use the packaged demo bootstrap:

```bash
orcho demos bootstrap golden-api
```

`orcho demos install golden-api` is accepted as the same operation.

From an existing source checkout, run the shell bootstrap script directly:

```bash
examples/scripts/bootstrap_demo_1a.sh
```

Do not clone this repository next to a `pipx install orcho` only to obtain the
demo assets; that creates two Orcho copies on the machine and makes it too easy
to confuse the installed CLI with source-checkout code.

Then paste the printed `orcho run ... --mock` command and inspect:

```bash
orcho evidence --workspace /tmp/orcho_demo_1a/workspace-orchestrator
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

The user-facing portal is **[docs.orcho.dev](https://docs.orcho.dev)** — start there.

The in-repo docs below are the contributor & deep reference: the canonical
engineering contracts the portal links into. Ordered from general to specific.

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
