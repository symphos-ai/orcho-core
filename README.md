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

No engine fork is required for project-specific context. Orcho starts with a
safe generic fallback, and `workspace init` creates a language-neutral plugin
scaffold. Completing that scaffold for the project is the recommended setup:
it gives Orcho explicit architecture context, file hints, and authoritative
verification policy instead of making every run rediscover them.

---

## Quick start — your existing repository

With Python 3.12+, `pipx`, and one supported coding-agent CLI on `PATH`,
install Orcho once and initialise it from inside the repository you already
have:

```bash
pipx install orcho

cd ~/www/my-project
orcho workspace init
orcho run --mock --task "Describe and implement one small change"
orcho status
```

`workspace init` does not move, copy, or modify the repository layout. It
registers the canonical project path and stores Orcho's control state in an
external managed workspace. Later CLI commands resolve that workspace from
the current project directory; no `--project` flag, environment script, or
dedicated parent folder is required.

The mock run exercises the delivery pipeline without calling a model. For a
real run, remove `--mock` and make sure at least one supported coding-agent CLI
is available on `PATH`.

**Detailed walkthrough:** [Getting started](docs/user/00_getting_started.md)

### Next step — a shared product workspace

The in-place flow above is the fastest way to start. For a long-lived product,
especially one split across repositories such as a backend and frontend, the
recommended second step is to keep the related repositories under one
intentional root and place the Orcho workspace there too:

```text
~/work/my-product/
├── backend/
├── frontend/
└── workspace-orchestrator/  # created by Orcho
```

If the repositories already share a parent, use it. If they do not, reorganise
them when that is practical; Orcho still accepts absolute paths, so this layout
is a best practice rather than a requirement.

Initialise the product root:

```bash
orcho workspace init ~/work/my-product
```

Then either run commands from `~/work/my-product`, where Orcho discovers the
workspace automatically, or activate it once in a Unix shell and run from any
directory:

```bash
source ~/work/my-product/workspace-orchestrator/orcho-env.sh
```

This gives mono-project and cross-project runs one place for aliases, policy,
history, evidence, and MCP configuration. Cross-project work can then name the
registered repositories explicitly:

```bash
orcho cross \
  --task "Change the API contract and update the frontend" \
  --projects backend frontend
```

`workspace init` registers the directory names as aliases, so repeating their
absolute paths is unnecessary. `--projects` remains explicit because one
workspace may contain more repositories than a particular change should touch.

See [Connecting your project](docs/user/03_workspaces.md) for the complete
shared-workspace setup and configuration precedence.

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

## Go deeper

The [getting-started guide](docs/user/00_getting_started.md) covers platform
prerequisites, MCP client setup, real provider runs, evidence inspection, and
the optional shared-root layout for intentional cross-project work.

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
cd ~/my-project
orcho run --task "Add input validation to /api/login"

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

## Configure the generated project plugin

`workspace init` prints the path to a generated plugin scaffold and its matching
agent-rule templates. The scaffold is deliberately inert: init has not
inspected the repository deeply enough to invent commands, environments, or
delivery policy safely.

Generic mode is sufficient for the first smoke run. For sustained use, copy
the generated scaffold to `your-project/.orcho/multiagent/plugin.py`, merge the
generated agent rules into the project's root instructions, and complete the
configuration from facts found in the repository:

```python
PLUGIN = {
    "name": "My Project",
    "language": "Python 3.12",
    "architecture": "REST API. Routes: app/routes/, services: app/services/.",
    "file_hints": ["app/routes/", "app/services/", "tests/"],
    "verification_envs": {"project": {"python": "python"}},
    "verification": {
        "default_env": "project",
        "commands": {"lint": {"run": ["python", "-m", "ruff", "check", "."], "cost": "fast"}},
        "gate_sets": {"hygiene": {"commands": ["lint"], "default_policy": "require"}},
        "selection": [{"always": ["hygiene"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["hygiene"], "action": "repair_loop"}],
    },
}
```

This declares a command, selects it, gives it a scheduled identity, lets Orcho
execute it, records an immutable receipt, and uses that receipt for readiness.
Cost is evidence metadata: `fast` is bounded deterministic local feedback,
`moderate` needs more setup or time, `slow` is broad or expensive, and
`unknown` has no reliable predictable cost evidence. It never shortcuts
selection, execution, policy, or action. See the practical [scheduled
verification guide](docs/guides/scheduled_verification.md).
Without a configured project plugin, Orcho still runs, but it falls back to
generic context and has no project-owned scheduled verification contract.
For the full workflow—including read-only fine-tune suggestions,
agent-assisted repository discovery, and the engineer approval boundary—see
[Configure the generated plugin scaffold](docs/user/03_workspaces.md#configure-the-generated-plugin-scaffold).

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
