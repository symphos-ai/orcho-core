# Full Orcho Architecture

## Concept: three layers

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI Layer          cli/orcho.py                                │
│  Command routing, arguments, entry point                        │
├─────────────────────────────────────────────────────────────────┤
│  Pipeline Layer     pipeline/                                    │
│  Phase orchestration, session management, checkpoints           │
│  ┌───────────────────────┐  ┌─────────────────────────────────┐ │
│  │ project_orchestrator  │  │ cross_project_orchestrator      │ │
│  └──────────┬────────────┘  └──────────────┬──────────────────┘ │
│             └──────────────┬───────────────┘                    │
│                   pipeline/engine/  (DRY core)                  │
├─────────────────────────────────────────────────────────────────┤
│  Agent Layer        agents/                                      │
│  Agent abstractions, providers, protocols                       │
│  ClaudeAgent (plan/run/hypothesize)  CodexAgent (review/qa)     │
├─────────────────────────────────────────────────────────────────┤
│  Core Layer         core/                                        │
│  Infrastructure: config, platform, io, logging, metrics         │
│  infra/ │ observability/ │ io/ │ context/                       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data flow: `orcho run`

```
cli/orcho.py::main()
  │
  └─► pipeline/project_orchestrator.py::run_pipeline()
        │
        ├── engine.init_session()            → RunContext
        ├── engine.setup_run_logging()       → runs/{ts}/output.log
        ├── load_plugin(project_dir)         → PluginConfig
        │
        ├── maybe_run_hypothesis()             [Phase 0.hypothesis, optional]
        │     └── claude.hypothesize()
        │
        ├── claude_plan.plan()               [Phase 0: PLAN]
        │     └── prompt: tasks/plan.md + plugin context
        │
        ├── codex.qa_plan()                  [Phase 0.5: validate_plan]
        │     └── prompt: tasks/validate_plan.md
        │
        │   [if QA fails → claude.replan() → Phase 0.fix REPLAN]
        │
        ├── claude.run(build_prompt)         [Phase 1: BUILD]
        │
        ├── codex.review_uncommitted()       [Phase 2: REVIEW]
        │
        ├── claude.run(fix_prompt)           [Phase 3: FIX × max_rounds]
        │
        ├── codex.review_uncommitted()       [Phase 4: final_acceptance]
        │
        └── engine.save_session()            → runs/{ts}/meta.json
```

---

## Data flow: `orcho cross`

```
pipeline/cross_project/orchestrator.py::run_cross_pipeline()
  │
  ├── engine.init_session()                  → RunContext (cross)
  ├── claude.plan(cross_plan_prompt)         → cross_plan.md + interface_contract.md
  │
  ├── for each project:                      [sequential]
  │     run_pipeline(project_dir, cross_plan=cross_plan.md)
  │
  └── codex.validate_contract(interface_contract.md)
```

---

## pipeline/engine/ — DRY core

Extracted to avoid duplication between `project_orchestrator` and `cross_project_orchestrator`:

```
pipeline/engine/
  context.py      — RunContext dataclass (all run parameters)
  session.py      — init_session(), save_session()
  run_logging.py  — setup_run_logging() — progress.log + output.log
  hypothesis.py     — run_hypothesis_loop(), maybe_run_hypothesis()
```

**RunContext** is an immutable dataclass passed through the whole pipeline:
```python
@dataclass(frozen=True)
class RunContext:
    run_id: str
    output_dir: Path
    plan_model: str
    implement_model: str
    repair_model: str
    review_model: str
    dry_run: bool
    max_rounds: int
    profile_name: str
    session_mode: SessionMode
```

---

## core/ — subdomains

```
core/
  __init__.py           — PACKAGE_ROOT: Path (single source of the package root path)
  infra/
    config.py           — AppConfig, models, timeouts, _find_binary(), _wrap_windows_cmd()
    platform.py         — engine_home(), runspace_dir(), _IS_WINDOWS, binary candidates
  observability/
    logging.py          — banner(), warn(), success(), color utilities
    metrics.py          — MetricsCollector, load_historical_runs()
    trace.py            — vtrace(), vdump(), vtimed() — verbose tracing
  io/
    retry.py            — call_with_retry(), RetryPolicy, error classification
    git_helpers.py      — has_uncommitted(), git_diff_stat()
    prompt_loader.py    — render_prompt() — 3-level prompt resolution
  context/
    __init__.py         — build_repo_map(), inject_context() — optional codemap
```

---

## agents/ — agents and providers

```
agents/
  __init__.py           — _stream_run() — subprocess launch with streaming
  protocols.py          — IAgentRuntime (Protocol)
  entities.py           — ImplementationPlan, ImplementationTask, ReviewResult
  providers/
    claude.py           — ClaudeAgent (plan + run + hypothesize, session chaining)
    codex.py            — CodexAgent (review, qa, validate_contract)
    _strategy.py        — MockAgentProvider, FailingMockProvider (for tests)
```

**Principle:** agents are thin wrappers around the CLI. No business logic.

---

## Prompt resolution (3 levels)

```python
# core/io/prompt_loader.py::render_prompt()
def render_prompt(name: str, project_dir: Path | None, **vars) -> str:
    # 1. project_dir/.orcho/multiagent/prompts/{name}.md
    # 2. workspace_dir()/.orcho/multiagent/prompts/{name}.md
    # 3. core.infra.paths.PROMPTS_DIR/{name}.md  ← always present
    template = _load_first_found(name, project_dir)
    return Template(template).safe_substitute(vars)
```

---

## Checkpoint store

`pipeline/checkpoint.py` — SQLite, stores the state of every phase.

```python
store = CheckpointStore(output_dir / "checkpoints.db")
store.save("build", status="ok", output=stdout)
# On --resume: skips phases with status "ok"
completed = store.load_completed()
```

---

## Design principles

1. **core.infra.paths** — single surface for package assets (`CONFIG_DIR`, `PROMPTS_DIR`) and the subprocess import root (`SOURCE_ROOT`)
2. **DRY engine** — code shared by the two orchestrators lives in `pipeline/engine/`
3. **Zero project context in core** — `core/` knows nothing about specific projects
4. **Protocol-based agents** — agents sit behind interfaces, easy to mock
5. **3-level prompts** — override without forking
6. **_wrap_windows_cmd** — the single place for Windows-specific CLI handling
