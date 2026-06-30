# ADR 0079 — Resume-artifact bootstrap for fresh-process resume

- Status: Accepted
- Date: 2026-06-10
- Relates to: ADR 0073 (implement-phase substance-repair handoff), ADR 0072
  (continue-with-waiver handoff action), ADR 0075 (event-sourced run state and
  terminal writes), ADR 0035 (terminal-status and resume observability)

## Context

A run can resume in a **fresh process** (MCP / Web, or a checkpoint resume
launched as a new subprocess) after an earlier phase already persisted its
durable output to the run directory. The in-memory `PipelineState` from the
original launch is gone, and the resume path does not re-run the producing
phase, so any later phase that needs that output finds it missing unless it is
lifted back from disk first.

The concrete bug this closes: the PLAN phase durably persists
`parsed_plan.json` (the round-trip-safe machine source — see
[`pipeline/plan_artifacts.py`](../../pipeline/plan_artifacts.py)), but on a
fresh-process resume that strips the plan loop (a `continue` /
`continue_with_waiver` decision on a plan handoff) or re-runs only the
incomplete subtasks (a `retry_feedback` decision on an implement handoff,
ADR 0073), `state.parsed_plan` is empty. The `subtask_dag` implement path then
halted with a generic "requires a parsed plan with at least one required
subtask" message — even though the plan was sitting on disk — leaving the
operator decision functionally hollow and the diagnosis opaque.

Two recovery sites had grown **independent copies** of the same load logic
(`load_parsed_plan_artifact` + `render_plan_markdown`): one in
`state_setup.hydrate_parsed_plan_from_output_dir` and one in
`handoff.rehydrate_parsed_plan`. Two loaders means two places to keep the
no-markdown-fallback invariant and the no-overwrite rule correct.

**The persisted shape constrains the required-signal.**
[`pipeline/checkpoint.py`](../../pipeline/checkpoint.py) stores an
**append-only `completed` list** of phase names. There is **no** separate
`skipped` set — `should_skip(name) == name in completed`. So the only durable,
observable signal that "a resume already finished PLAN" is `'plan' in
completed`. For an **in-process** handoff resume, the plan/validate_plan phases
are marked completed *later* in the same process — after `state` was built — so
`checkpoint.completed` does not yet carry them when state is constructed; that
path must carry the same required-signal by a different, in-memory means.

The invariant we want to make explicit and enforce: **if a resume leaves a
phase behind, that phase's durable outputs are restored into the runtime state
before any dependent phase runs** — without re-running the producing phase, and
without silently degrading to a non-round-trip-safe source.

## Decision

Centralise the lift behind a small, tested registry of
**`ResumeArtifactSpec`** entries in
[`pipeline/project/resume_artifacts.py`](../../pipeline/project/resume_artifacts.py),
with `parsed_plan` as the first (and currently only) production spec.

### `ResumeArtifactSpec` + generic runner

Each spec owns, declaratively:

- `name` — stable identity, used as the provenance key;
- `phase` — the producing phase;
- `artifact` — the artifact filename under `run_dir` (used read-only to tell a
  *missing* file from a *corrupt* one);
- `load(run_dir) -> value` — raises a domain error on missing/corrupt (for
  `parsed_plan`: `load_parsed_plan_artifact` raising `ParsedPlanArtifactError`);
- `project(state, value)` — seed the value onto `state` (and record
  loaded-provenance);
- `required_when(ctx) -> bool` — True when the artifact must be present for the
  resume (`parsed_plan`: `'plan' in ctx.completed_phases`);
- `already_present(state) -> bool` — True when state already carries the value
  (an explicit `--from-run-plan` plan, or a same-process resume), so the runner
  skips without overwriting.

`bootstrap_resume_artifacts(state, run_dir, *, completed_phases, specs=REGISTRY)`
iterates specs **without branching on any spec's name** and classifies each into
one of **six mutually-exclusive categories**: `loaded`,
`skipped_already_present`, `missing_optional`, `missing_required`,
`corrupt_optional`, `corrupt_required`. It never creates files or directories;
`run_dir is None` short-circuits to an empty result with no mutation.

### Owned marker `RESUME_PLAN_REQUIRED_KEY`

`resume_artifacts.py` is the **single owner of the marker name**
(`state.extras['resume_plan_required']`, a bool). The runner sets it whenever a
spec is required for the resume. At this stage there is one required spec, so
the marker means "this resume needs a parsed plan".

- **Writers:** the bootstrap runner (driven by `state_setup`), and the three
  in-process handoff strip/retry sites in `pipeline/project/handoff.py` (the
  `continue` plan branch, the `continue_with_waiver` plan branch, and the
  implement-retry arm) — those set the marker explicitly because they mark the
  plan phases completed only later in-process.
- **Reader:** the `subtask_dag` implement guard.

### Provenance discipline

Provenance is written to `state.extras['resume_artifacts'][name]` **only** for:

- a successful load → `{'source': 'artifact'}` (via `project_parsed_plan`); and
- a *required* failure → `{'status': 'missing' | 'corrupt'}`.

Optional failures (fresh run, no artifact, not required) write **nothing** —
`state.extras` stays byte-identical to the pre-bootstrap path, preserving
existing snapshot/boundary tests (e.g. `test_verification_contract_projection`).

### Authoritative error in the requiring phase

`build_pipeline_state` does **not** raise on a missing/corrupt required artifact
(whole-plan implement resume does not require a typed plan everywhere). The
authoritative operator error lives in the **requiring** phase: `subtask_dag`'s
empty-plan guard. When `state.parsed_plan is None` **and** the marker is set, it
emits an instructive message naming `<run_dir>/parsed_plan.json` and
distinguishing *missing* from *unreadable (corrupt)* via the recorded
provenance; without the marker it keeps the prior generic line.

### Deduplicated loader, no markdown fallback

`handoff.rehydrate_parsed_plan` now delegates to the shared
`load_and_project_parsed_plan(state, run_dir)` projector; the duplicate
`load_parsed_plan_artifact` + `render_plan_markdown` body is gone. The
no-markdown-fallback invariant from `plan_artifacts.py` is preserved in exactly
one place: a corrupt `parsed_plan.json` is a `corrupt_*` category and surfaces
the guard error — it is **never** silently reconstructed from the human
`plan_<run>_r<n>.md` projection, which is not round-trip-safe.

### MCP validation

This change touches no runtime/gate wire schema, profile shape, mode flags, or
gate primitives — it is internal resume plumbing inside `orcho-core`. No
`orcho-mcp` update is required.

## Consequences

- A fresh-process resume that leaves PLAN behind recovers the durable plan and
  proceeds into implement instead of halting with an opaque generic message.
- A missing/corrupt required plan produces a single, instructive,
  operator-actionable error from the phase that actually needs the plan.
- There is exactly one owner of resume parsed-plan loading and one owner of the
  marker name; the no-overwrite (`--from-run-plan`, same-process) and
  no-markdown-fallback invariants live in one place.
- Non-resume and fresh-run paths are a strict no-op: no marker, no provenance,
  no directory creation, no dependence of fresh plan generation on a stale
  artifact.

## Generalization points

The runner is generic; future phases that produce a typed durable output
register their own `ResumeArtifactSpec` rather than re-deriving "required but
absent" from a bare `None` check. A future spec that needs a distinct
"required but absent" operator signal extends the marker contract rather than
spelling a new string literal across modules. Candidate future specs are
tabulated in
[`docs/architecture/run_state.md`](../architecture/run_state.md#resume-artifact-bootstrap).

## See also

- [Run State](../architecture/run_state.md#resume-artifact-bootstrap) — the
  invariant, the persisted-source note, and the marker contract
- [`pipeline/plan_artifacts.py`](../../pipeline/plan_artifacts.py) — the
  round-trip-safe `parsed_plan.json` source and the no-markdown-fallback rule
- [ADR 0073](0073-implement-phase-substance-repair-handoff.md) — the
  implement-retry resume arm that consumes the rehydrated plan
