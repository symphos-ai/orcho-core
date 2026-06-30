# Typed silent boundary — consumer reference

> The canonical pattern for driving an orcho-core run from code
> (SDK, MCP, integration harness, embedded UI) instead of from a
> terminal. Reads in 2–3 minutes; the executable companion lives in
> `tests/integration/{project,cross}/test_typed_boundary_consumer.py`.

## When to use what

| Caller shape | Use |
|---|---|
| Human at a terminal — wants the live transcript | CLI: `orcho run` / `orcho cross` |
| In-process Python code — wants structured state | Typed boundary: `run_project_pipeline(ProjectRunRequest(...))` / `run_cross_project_pipeline(CrossRunRequest(...))` |
| Out-of-process integration via subprocess | Use the CLI — but consume `meta.json` / `events.jsonl` afterwards, NOT the transcript |

The typed boundary is **silent-capable**. With
`presentation=PresentationPolicy.SILENT, no_interactive=True` the run
produces zero stdout and zero stderr while every persisted artifact
(`meta.json`, `events.jsonl`, `progress.log`, checkpoint, worktree
teardown, mirror) lands byte-identical to the TERMINAL path. This is
the seam that lets a consumer treat orcho as a library — no log
interleaving, no terminal escape sequences, no parsing.

ADR pointers: [ADR 0042][a42] (typed project boundary),
[ADR 0046][a46] (silent presentation policy),
[ADR 0047][a47] (typed cross boundary).

[a42]: ../adr/0042-project-pipeline-application-boundary.md
[a46]: ../adr/0046-silent-app-level-boundary.md
[a47]: ../adr/0047-cross-project-application-boundary.md

## Single-project consumer

```python
from pathlib import Path

from agents.runtimes import MockAgentProvider  # or your real provider
from pipeline.project.app import run_project_pipeline
from pipeline.project.types import (
    PresentationPolicy,
    ProjectRunRequest,
)

request = ProjectRunRequest(
    task="describe the change you want",
    project_dir="/abs/path/to/project",      # consumer-owned checkout
    output_dir=Path("/abs/path/to/runs/my-run"),
    max_rounds=1,
    profile_name="small_task",               # or "feature" / "auto-detect"
    provider=MockAgentProvider(latency=0.0), # swap for the real runtime
    presentation=PresentationPolicy.SILENT,
    no_interactive=True,                     # required by SILENT
)

result = run_project_pipeline(request)

# Structured state — the contract surface:
assert result.session["status"] == "done"
run_dir = result.output_dir
run_id  = result.run_id
```

### Hard invariant

`presentation=SILENT` requires `no_interactive=True`. The dataclass
`__post_init__` raises `ValueError` if you forget — there is no
silent widening of operator prompts. If your consumer can't supply
`no_interactive=True`, you can't run silent.

### What you read after the call

| Source | Shape | Use it for |
|---|---|---|
| `result.session` | `dict` (same dict as `meta.json`) | Immediate read of final status, halt reason, failure block, handoff payload |
| `result.output_dir` | `Path` | Where every persisted artifact lives; anchor for tailing |
| `result.run_id` | `str` (session_ts) | Correlation key across logs / events / artifacts |
| `<run_dir>/meta.json` | `dict` | Durable post-process replay of `result.session` |
| `<run_dir>/events.jsonl` | line-delimited JSON | Structural progress: `run.start`, `phase.start`, `phase.end`, `run.end`, plus any handoff / gate events |
| `<run_dir>/progress.log` | human-readable lines | Diagnostic tail (NOT a primary parsing target) |

The canonical event spine is:

```
run.start → (phase.start → phase.end)+ → run.end
```

Exactly one `run.end` lands under SILENT — the silent finalization
service is the only emitter (the terminal wrapper, when active,
consumes the structured result and re-renders chips, but does NOT
re-emit `run.end`). Consumers that count completions can rely on
this.

## Cross-project consumer

```python
from pathlib import Path

from pipeline.cross_project.app import run_cross_project_pipeline
from pipeline.cross_project.app_types import CrossRunRequest
from pipeline.presentation import PresentationPolicy

request = CrossRunRequest(
    task="ship the same change across both services",
    projects={
        "api": Path("/abs/path/to/api"),
        "web": Path("/abs/path/to/web"),
    },
    output_dir=Path("/abs/path/to/runs/cross-run"),
    cross_mode="full",
    profile_name="code_review",              # or "feature" / plugin profile
    provider=my_provider,
    presentation=PresentationPolicy.SILENT,
    no_interactive=True,
)

result = run_cross_project_pipeline(request)

assert result.session["status"] == "done"
run_dir = result.output_dir
run_id  = result.run_id
```

### Cross-only invariant — child SILENT is automatic

The cross body fans out per-alias dispatch. Every child request the
cross body builds carries `presentation=SILENT, no_interactive=True`
regardless of the cross-level presentation policy. Cross consumers
do not thread the child seam — the cross body owns it. ADR 0046
Phase D.

This means a TERMINAL cross run still has SILENT children: child
transcripts never interleave into the cross transcript. The cross
transcript carries only cross-owned banners (`▶ SUB-PIPELINE`,
`[CONTRACT_CHECK]`, `[DONE]`, `Projects: N | Rounds each: M`).

### What you read after the cross call

Same shape as the project call, with the per-alias children recorded
under `result.session["phases"]["projects"][<alias>]`:

```python
projects_log = result.session["phases"]["projects"]
for alias, entry in projects_log.items():
    print(alias, entry["status"])   # done / failed / awaiting_phase_handoff / ...
```

`meta.json` mirrors this. Child events tagged with `phase_key` and
`project_alias` land in the cross-level `events.jsonl` so a single
tail consumer sees the whole run.

## Anti-patterns

### Do not parse stdout

```python
# ❌ wrong — fragile, locale-dependent, breaks under SILENT.
import subprocess
output = subprocess.run(["orcho-run", ...], capture_output=True, text=True)
if "DONE" in output.stdout:
    ...

# ✅ right — typed boundary, structured state.
result = run_project_pipeline(ProjectRunRequest(... SILENT ...))
if result.session["status"] == "done":
    ...
```

The transcript markers (`[PLAN]`, `[IMPLEMENT]`, `[DONE]`,
`Run dir: …`, `Session: …`, `Usage: …`) are **operator-facing
prose**. They have no stability contract and are silenced under
SILENT. Parse `meta.json`, not stdout.

### Do not skip `no_interactive=True`

```python
# ❌ wrong — ValueError at construction.
ProjectRunRequest(... presentation=SILENT)            # no_interactive=False (default)
ProjectRunRequest(... presentation=SILENT, no_interactive=False)
```

Interactive prompts are terminal-by-definition; SILENT requires the
caller to commit to the headless contract up-front.

### Do not depend on `progress.log` for parsing

`progress.log` is a human-readable diagnostic tail. Its line format
is not a contract. Use `events.jsonl` for structural progress and
`meta.json` for final state.

### Do not re-derive status from event counts

Status lives in one place: `result.session["status"]` /
`meta.json["status"]`. Don't recompute it by counting `phase.end`
events or scanning for `error_type`. The finalization service is the
single owner of the status decision; consumers read it.

## Resume, handoff, and failure — also structured

Every non-done terminal status carries `halt_reason` (ADR 0035) plus
a structured payload:

| `session["status"]` | What to read |
|---|---|
| `done` | `result.session["phases"]` for per-phase summary |
| `failed` | `result.session["failure"] = {"type": ..., "error": ..., "phase": ...}` plus `halt_reason="phase_failure:<type>"` |
| `awaiting_phase_handoff` | `result.session["phase_handoff"]` payload — phase, evidence path, prompt seed |

The `run.end` event in `events.jsonl` carries `payload.status` and,
on the failure path, `payload.error_type`. Consumers that surface
completion to a UI can read either source.

## Reference smoke

The executable companions to this doc:

* `tests/integration/project/test_typed_boundary_consumer.py`
* `tests/integration/cross/test_typed_boundary_consumer.py`

Each one walks the four-step contract end-to-end against
`MockAgentProvider` / a scripted provider. They double as the
regression net — drift in the consumer-facing shape trips them
before any deeper unit test.

## Where next

* [Architecture overview](../architecture/overview.md) — the
  top-level mental model, including the typed-boundary stack.
* [Phase lifecycle](../architecture/phase_lifecycle.md) — what
  events fire and when.
* [Session shape](../architecture/session_shape.md) — what
  `meta.json` / `result.session` actually carries.
