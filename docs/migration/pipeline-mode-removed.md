# Migration: `--mode` → `--profile` (Phase 6)

> **TL;DR:** The `PipelineMode` enum was deleted in Phase 6 of the
> pipeline architecture redesign. The `--mode` CLI flag is gone;
> `--profile` is the single dispatch knob. `--skip-plan` is gone too —
> use `--profile task` for the build-only flow.
>
> **No backward compat.** Per the orcho `feedback_no_backcompat_ceremony`
> policy (solo project, no legacy aliasing), Phase 6 is a clean break.
> Update scripts / supervisors / aliases to the new flag shape.

## CLI: `orcho run`

| Pre-Phase-6 | Phase 6+ | Note |
|---|---|---|
| `--mode full` | (default; `--profile advanced`) | "advanced" replaces FULL |
| `--mode plan` | `--profile plan` | direct rename |
| `--mode review` | `--profile review` | direct rename |
| `--mode task` | `--profile task` | direct rename |
| `--mode full --skip-plan` | `--profile task` | task profile = build-only |
| (env) `ORCHO_PIPELINE=lite` | `--profile lite` | env override still works |

## CLI: `orcho cross`

`orcho cross --mode {full,plan}` is **unchanged** — that flag is the
cross-orchestrator-only "full cross run vs plan-only cross run"
concept, not a per-project pipeline mode. It survives Phase 6 with
the same semantics. Internally it's threaded as `cross_mode` to
distinguish from per-project profile.

## Python API: `run_pipeline`

```python
# Pre-Phase-6
from pipeline.project_orchestrator import PipelineMode, run_pipeline
run_pipeline(
    task="...",
    project_dir="...",
    pipeline_mode=PipelineMode.FULL,
    skip_plan=False,
)

# Phase 6+
from pipeline.project_orchestrator import run_pipeline
run_pipeline(
    task="...",
    project_dir="...",
    profile_name="advanced",   # or "lite" / "enterprise" / "plan" / "review" / "task"
)
```

The `PipelineMode` import is gone. `skip_plan` is gone.

## Python API: `run_cross_pipeline`

```python
# Pre-Phase-6
from pipeline.project_orchestrator import PipelineMode
from pipeline.cross_project.orchestrator import run_cross_pipeline
run_cross_pipeline(
    task="...", projects={...},
    pipeline_mode=PipelineMode.PLAN,
)

# Phase 6+
from pipeline.cross_project.orchestrator import run_cross_pipeline
run_cross_pipeline(
    task="...", projects={...},
    cross_mode="plan",        # "full" or "plan" — cross-orchestrator-only flag
)
```

## argv builder: `pipeline.argv.build_orch_argv`

```python
# Pre-Phase-6
from pipeline.argv import build_orch_argv
argv = build_orch_argv(
    project="/p", task="...",
    mode="task",        # legacy
    skip_plan=False,
)

# Phase 6+
argv = build_orch_argv(
    project="/p", task="...",
    profile="task",            # per-project v2 dispatch knob
    cross_mode="full",         # cross-orchestrator-only flag (default "full")
)
```

The `mode=` and `skip_plan=` kwargs are gone. The new `profile=` is
the per-project dispatch knob; `cross_mode=` is threaded through to
the cross-only `--mode` argv flag (separate concept).

## Session shape: `meta.json`

```diff
 {
   "task": "...",
   "project": "...",
   "model": "...",
-  "pipeline_mode": "full",
+  "profile": "advanced",
   "session_mode_requested": "auto",
   "timestamp": "...",
   "status": "...",
   "phases": { ... }
 }
```

`session["pipeline_mode"]` is gone; `session["profile"]` carries the
profile name string. Tooling (`orcho status`, dashboards,
acceptance fixtures) reads the new key.

For cross-runs: `session["cross_mode"]` carries `"full"` or `"plan"`
(separate from per-project profile, mirrors the cross-orchestrator's
own concept).

## Checkpoint config

```diff
 {
   "task": "...",
   "project": "...",
   "model": "...",
-  "pipeline_mode": "full",
+  "profile": "advanced",
   "max_rounds": 1,
   "max_plan_rounds": 1,
   "block_on_qa_reject": false,
 }
```

Resume reads the profile name from checkpoint config.

## orcho-mcp supervisor

`RunsSupervisor.spawn(profile="advanced", ...)` — `mode` parameter
gone, `profile` is the single dispatch knob. Threaded through both
`--profile` argv flag and `ORCHO_PIPELINE` env var (the env var
acts as an explicit override for sub-pipelines that bypass argv
parsing).

`RunsSupervisor.resume(run_id, profile="task")` — was `mode="task"`.
Default unchanged ("task" is the canonical QA-approval continuation
profile).

## Why no aliasing?

Per the `feedback_no_backcompat_ceremony` orcho memory: solo
project, no parallel paths, no `if mode is None: mode = profile`
ceremony. Aliasing `--mode` → `--profile` would mean two flags
forever. Cleaner to break + migrate now.

If you have scripts that pass `--mode <X>`, the mechanical fix is:

```bash
# Before
orcho run --task "..." --project /p --mode plan
orcho run --task "..." --project /p --mode full --skip-plan

# After
orcho run --task "..." --project /p --profile plan
orcho run --task "..." --project /p --profile task
```

## See also

- `docs/reference/profile_schema.md` — full `Profile` JSON schema
  + the 6 shipped profiles
- `docs/reference/cli.md` — `orcho run` flag reference
- `docs/adr/0001-pipeline-redesign.md` — overall redesign rationale
- `docs/adr/0009-flat-cli-namespace.md` — why `--profile <name>` is
  flat (no nested `--kind X --variant Y`)
