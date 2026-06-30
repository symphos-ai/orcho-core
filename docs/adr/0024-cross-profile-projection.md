# ADR 0024 — Cross-aware profile projection

## Status

Accepted.

## Context

`orcho cross` previously ignored `--profile` for everything past the
cross-level plan. Child sub-pipelines were forced through a hardcoded
`CROSS_SUB_PROFILE = "task"` constant, and the cross-level
`cross_plan` / `cross_validate_plan` / `contract_check` steps lived in
bespoke imperative code instead of being driven by the requested
profile. Picking `--profile advanced` therefore changed nothing
meaningful inside the children, and there was no way to declare which
workflow steps belong at the cross level versus inside each child
project.

## Decision

Profiles carry optional per-step `cross` policy. The cross runner
projects the requested profile into two lists — `global_steps`
(executed once at the cross level) and `project_steps` (executed inside
each child) — and dispatches each side appropriately.

### Data model

```python
class CrossScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"
    BOTH = "both"
    SKIP = "skip"

@dataclass(frozen=True)
class CrossStepPolicy:
    scope: CrossScope
    handler: str | None = None
```

`PhaseStep` gains `cross: CrossStepPolicy | None = None`. The field is
optional: profiles without it remain valid for mono runs and are
rejected only when the cross projector encounters them.

`CrossStepPolicy.handler` is **dispatch metadata only**. It does not
rename `PhaseStep.phase`. The cross runner uses `handler` to look up a
cross-level function (`cross_plan`, `cross_validate_plan`) while keeping
the semantic phase name intact so loop predicates like
`until: validate_plan.approved` continue to evaluate correctly.

### Projection rules

* `cross is None` on any `PhaseStep` → projection error in cross mode.
* `scope=global` → step goes to `global_steps` only.
* `scope=project` → step goes to `project_steps` only.
* `scope=both` → step goes to both lists (rare; reserved for steps that
  genuinely fan out).
* `scope=skip` → omitted entirely.
* A `LoopStep`'s inner steps must agree on scope. Mixed scopes raise
  `CrossProjectionError`.
* Coherence: if `project_steps` contains `implement` or
  `repair_changes` but `global_steps` has no `plan` / `validate_plan`
  step to produce a handoff, projection fails. This makes
  `orcho cross --profile task` an explicit error.

### Handoff

After cross-level planning is approved, the cross runner writes
`<run_dir>/<alias>/implementation_handoff.{md,json}` for every child
and passes the markdown path to `run_pipeline(handoff_path=...)`.

`run_pipeline` gains:

* `profile_obj: Profile | None = None` — bypass name resolution.
* `plan_source: "local" | "cross" | "none" = "local"` — declarative
  context for the run.
* `handoff_path: str | None = None` — required iff `plan_source="cross"`
  AND the resolved profile contains `implement` or `repair_changes`.
  Review-only profiles run without a handoff. `final_acceptance` does
  not require it.

Phase handlers read the validated handoff text from
`state.extras["cross_handoff"]` and pass it as `handoff_contract` to
the pure prompt builders (`build_prompt`, `review_focus`, `fix_prompt`).
The handoff is prepended **before** the existing plan contract so the
agent reads cross context first.

### Cross-only terminal gate

`contract_check` is **not** a step in shipped profile JSON. It is a
cross-only terminal gate the cross runner appends after all project
pipelines finish. Mono runs never invoke it. Parse errors or rejected
verdicts set `session.status="failed"` and the CLI returns exit code 1.

### Effective profile naming

`meta.json` / event payloads / headers expose:

* `profile=<requested>` (e.g. `advanced`)
* `plan_source=<local|cross|none>`
* `projected_profile=<requested>#project` when projection occurred

`task` is never surfaced as the effective profile.

## Consequences

* Single workflow knob: `--profile` controls both levels via projection.
  No `--sub-profile`.
* Profiles without `cross` metadata stay valid for mono runs. The cross
  runner rejects them with an actionable error.
* The `task` scoped profile is intentionally rejected for cross mode
  by the coherence rule. Mono `orcho run --profile task` is unchanged.
* `contract_check` failures are no longer silent. Parse errors and
  rejected verdicts fail the cross run.
* No backcompat ceremony: `CROSS_SUB_PROFILE` was deleted, not flagged
  behind a compat switch.

## Out of scope

Structured handoff extraction (`global_interface_contract`,
`execution_order`, `dependencies`, etc.) is deferred to a follow-up that
designs a parser-friendly cross-plan grammar. v1 handoffs carry
`full_cross_plan_markdown` + `project_subtask` and the agent infers the
rest.
