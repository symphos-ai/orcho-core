# Profile JSON Schema (v2)

> Phase 5 reference — parser + shipped profiles + v2 runtime dispatch.
> The legacy flat-shape file and v1 JSON loader were removed in Phase
> 5d-5; normal pipeline execution now resolves and walks this v2 schema.

orcho ships profile authoring through JSON so customer plugins can
declare pipelines without writing Python. Phase 5a introduces the
**v2** schema (two-axis `kind` × `variant`, `PhaseStep` / `LoopStep`
nested objects). Phase 5d makes v2 the default runtime path; the legacy
**v1** flat-shape (`{name: [phase_name, ...]}`) was removed from the
JSON authoring surface in Phase 5d-5.

> **Stage C semantic identity fields.** The built-in profiles are keyed by the
> nine semantic work kinds and carry explicit identity fields —
> `semantic_profile`, `default_mode`, `recipe_kind` — described under
> [Semantic identity fields](#semantic-identity-fields) below. The inert
> run-shape value objects (`SemanticProfile`, `OperatingMode`,
> `OperatingModePolicy`, `RunShape` in `pipeline/runtime/run_shape.py`) remain
> runtime types; `operating_mode` and `run_shape` are **not** profile-JSON
> keys. See
> [semantic_profiles_alignment.md](../architecture/semantic_profiles_alignment.md)
> for the live semantic surface and the deferred resolver/SDK/MCP work.

## Top-level shape

```json
{
  "<profile_name>": {
    "kind":        "full_cycle" | "scoped" | "custom",
    "variant":     "<axis-specific>" | null,
    "semantic_profile": "feature" | "small_task" | ... | null,
    "default_mode":     "fast" | "pro" | "governed" | null,
    "recipe_kind":      "full_cycle" | "focused" | "internal" | null,
    "description": "...",
    "change_handoff": "uncommitted" | "commit" | "commit_set",
    "implementation_execution": "whole_plan" | "subtask_dag",
    "steps":       [<step>, ...]
  },
  "<another_name>": { ... }
}
```

Underscore-prefixed top-level keys (e.g. `_comment`) are silently
ignored — useful for inline documentation in the JSON file.

Unknown keys inside profile objects, steps, loops, quality gates, or
human-review blocks are rejected. This keeps typos like
`quality_gate` (singular) from silently disabling verification.

`change_handoff` is optional. When omitted, Orcho reads
`AppConfig.pipeline.change_handoff` (default: `uncommitted`). The
profile-level value wins when present.

`implementation_execution` is optional. When omitted, Orcho reads
`AppConfig.pipeline.implementation_execution` (default: `whole_plan`). The
profile-level value wins when present. The shipped `feature` and `refactor`
work kinds select `subtask_dag`: the implement phase executes
`ParsedPlan.subtasks` as tracked delivery units and records per-subtask
receipts. The first `subtask_dag` runtime slice is sequential (`concurrency=1`
in implement metadata); do not author a separate `sequential_subtasks` mode.

## Semantic identity fields

The built-in profiles declare their semantic identity explicitly. These are the
source of a built-in's identity — **`variant` is not** (see the typology note
below).

| Field | Values | Meaning |
|-------|--------|---------|
| `semantic_profile` | `feature`, `small_task`, `complex_feature`, `planning`, `delivery_audit`, `code_review`, `research`, `refactor`, `migration`, or `null` | The goal-shaped work kind. For built-ins it equals the profile key. Internal `task` / `correction` leave it `null`. |
| `default_mode` | `fast`, `pro`, `governed`, or `null` | The default `OperatingMode` (`work_mode`) a run takes when no explicit override is given. Built-in defaults are deterministic (see [semantic_profiles_alignment.md](../architecture/semantic_profiles_alignment.md)); `governed` is never a built-in default. |
| `recipe_kind` | `full_cycle`, `focused`, `internal`, or `null` | The recipe breadth tag. `internal` marks profiles the fresh-run picker never offers (`task` / `correction`). |

All three are optional and validated at load time — an unknown
`semantic_profile` / `default_mode`, or a `recipe_kind` outside the set, raises
`ProfileLoadError`. Plugin / custom profiles that omit them parse unchanged.

The **effective** `work_mode` for a run is the explicit override (CLI
`orcho run --mode {fast,pro,governed}`, or an explicit project/contract
`work_mode`) when set, otherwise the profile's `default_mode`.
`orcho run --profile auto-detect` is a selector token, not a profile: it
resolves to a concrete semantic work kind and mode through the configured
confidence/fallback policy, then normal profile dispatch continues.

## Two-axis typology (`kind` × `variant`)

`kind` × `variant` is the legacy depth/target typology, retained for
plugin / custom profiles. **Built-in profiles no longer use `variant` for
semantic identity** — they ship as `kind: custom` (no `variant`) and declare
`semantic_profile` / `recipe_kind` instead.

| `kind` | Allowed `variant` values | Use |
|--------|--------------------------|-----|
| `full_cycle` | `lite`, `advanced`, `enterprise` | Complete dev cycle, varies by depth (plugin/custom typology) |
| `scoped` | `plan`, `review`, `task` | Partial workflow, varies by target (plugin/custom typology) |
| `custom` | `null` or any string | No further constraints; the built-in semantic work kinds use this |

`Profile.__post_init__` rejects mismatched combinations
(e.g. `kind=full_cycle, variant=plan` → `ValueError`).

## Change handoff

`change_handoff` controls the contract between authoring phases
(PLAN / DECOMPOSE / REPLAN / BUILD / FIX / DAG subtasks) and review
phases (REVIEW / FINAL_ACCEPTANCE):

| Mode | Authoring contract | Review target contract |
|------|--------------------|------------------------|
| `uncommitted` | Leave task changes in the working tree; do not stage/commit unless explicitly asked | Review `git status`, `git diff`, and relevant untracked files |
| `commit` | Produce exactly one task-relevant commit when code/test changes are made | Review the latest task commit and check for leftover working-tree changes |
| `commit_set` | Produce a small coherent set of task commits | Review the task commit set/range and check for leftover working-tree changes |

This is not a prompt-template concern. The selected mode is appended as
system-owned `change_handoff` / `review_target` blocks so project prompt
overrides cannot accidentally change the handoff semantics.

## Steps

A step is either a **PhaseStep** (one handler invocation) or a
**LoopStep wrapper** (retry block).

### PhaseStep

```json
{
  "phase":         "<registered phase name>",
  "execution":     "linear" | "<plugin-mode-name>"
                   | {"mode": "...", "session_split": "...", ...},
  "skill":         "<registered skill name>",
  "effort":        "low" | "medium" | "high",
  "overrides":     {"runtime": "..."},
  "prompt":        {"role": "...", "task": "...", "format": "..."},
  "hypothesis":    {"attempts": 1, "format": "compact"},
  "quality_gates": [<QualityGate>, ...],
  "human_review":  {<HumanReview>},
  "cross":         {"scope": "...", "handler": "..."}
}
```

Only `phase` is required. `execution` defaults to `"linear"` and accepts
either a bare string or an `ExecutionPolicy` object (see
[Execution policy](#execution-policy)). `execution: "linear"` is
shorthand for `execution: {"mode": "linear"}`. The string form remains
the canonical authoring shape for shipped pipeline modes (`linear`) and
plugin-registered modes such as `parallel_review`; the object form is
required only when a step opts into the `session_split` knob or a future
reserved field. Dispatch validates `execution.mode` against
`LifecycleContext.execution_mode_registry`.

Implement subtask delivery is not a `PhaseStep.execution` mode: the
semantic/profile resolver selects `whole_plan` vs `subtask_dag` through
`OperatingModePolicy.implementation_execution` / profile or pipeline
`implementation_execution` (see ADR 0067). `PhaseStep.execution` accepts
`linear` (built-in) plus any plugin-registered mode; unknown modes are
rejected.

#### Execution policy

```json
"execution": {
  "mode":               "linear" | "<plugin-mode-name>",
  "session_split":      "stateless" | "per_phase" | "per_role" | "common",
  "session_continuity": "fresh_only" | "loop_continue" | "same_zone_continue",
  "read_only":          <reserved>,
  "join":               <reserved>,
  "surfaces":           <reserved>
}
```

| Field | Meaning |
|---|---|
| `mode` | Same domain as the string form. Required when the object form is used. |
| `session_split` | Prompt-session policy knob. Selects the scope under which prompt-session state and physical session keys are reused. Defaults to `per_phase`. |
| `session_continuity` | ADR 0113 per-phase continuity policy. Selects whether a phase **resumes its own prior session** on a repeat invocation / loop round. Orthogonal to `session_split` (see below). `null`/absent means "no per-step preference"; the resolver supplies the role default. |
| `read_only` | Reserved for the fanout-review milestone — must be unset (or `null`) on shipped profiles. |
| `join` | Reserved for the fanout-review milestone — must be unset (or `null`) on shipped profiles. |
| `surfaces` | Reserved for the fanout-review milestone — must be omitted or an empty list on shipped profiles. A non-empty list is rejected at load time until the fanout runtime lands. |

`session_split` values:

| Value | Behavior |
|---|---|
| `stateless` | No reusable physical session. Every invocation renders the full prompt; no prompt-session state is recorded. |
| `per_phase` | (Default.) One physical session per phase per (`run_id`, runtime, model). Multi-round loops resume their prior session and the M6 selector renders a delta on round 2+. |
| `per_role` | One physical session per prompt role per (`run_id`, runtime, model). The step **must** declare `prompt.role` explicitly — the loader rejects `session_split=per_role` without a prompt role on the same step (`per_role` would otherwise silently degrade to `per_phase` under a different label). |
| `common` | One physical session per run per (runtime, model). Phases that share runtime and model share prompt-session state for the whole run. |

`session_continuity` values (ADR 0113):

| Value | Behavior |
|---|---|
| `fresh_only` | Always start a fresh session; the compact handoff carries prior context on the prompt. No follow-on signal resumes. (Shipped default for `review_changes` — its measured win.) |
| `loop_continue` | Resume the prior session on round 2+ of the same phase/role loop; round 1 is fresh. (Shipped default for `plan` / `validate_plan` — restores the pre-0113 replan/re-validate resume.) |
| `same_zone_continue` | Resume only for a same-write-zone edit follow-on (CHAIN repair reusing the implement worktree/session); a cross-zone follow-on is fresh. (Shipped default for `implement` / `repair_changes`.) |

`session_split` and `session_continuity` are **orthogonal axes** set
independently in the same `execution` block: `session_split` decides *how* a
session is shared *across phases* in one pass (the physical-session key scope),
while `session_continuity` decides *whether* a phase resumes *its own* prior
session on a repeat invocation / loop round. A profile may freely combine, e.g.
`{"session_split": "common", "session_continuity": "loop_continue"}`. Auxiliary
invocations (companion / contract re-emit / audit / verification / boundary) are
always fresh by invocation shape and resolved in code, not declared here.
`session_continuity` is internal-only — it is not surfaced by any SDK
profile-listing wire (like `session_split`, it is consumed by the runtime).

`fanout_review` is **not** an `execution.mode` value yet. Any profile
that sets a non-empty `surfaces` list or `read_only`/`join` is
rejected by the loader (`ExecutionPolicy.__post_init__`) — the
fanout-review milestone will lift those reservations together with
the runtime that implements them.

Plugin-registered execution modes remain loadable under both the string and
object forms once registered on the lifecycle registry. Implement subtask
delivery is not a profile-step execution mode; use
`pipeline.implementation_execution="subtask_dag"` or the semantic profile
policy field instead.

`prompt.role` is a prompt persona file under `_prompts/roles/`. It is
required when a step declares a `prompt` block; omitting the whole
`prompt` block lets the phase builder use its code-owned default.

`hypothesis` is an optional object on a `plan` step. Omitting it or
setting `attempts: 0` disables the pre-plan hypothesis prelude;
`attempts: 1` runs one attempt, `attempts: 2` runs two attempts, and so
on. `format` is optional: when present, the hypothesis prompt and its QA
review use that format; when omitted, they inherit the plan step's
`prompt.format`. CLI/API overrides may still force the prelude on or off
for a run.

### LoopStep

```json
{"loop": {
   "steps":                 [<PhaseStep>, ...],
   "until":                 "<phase>.<field>" | "not <phase>.<field>",
   "max_rounds":            <int>,
   "round_extras_key":      "<extras key>",
   "oscillation_halt_after": <int> | null
 }}
```

The wrapper `{"loop": {...}}` disambiguates from PhaseStep at the JSON
layer. Inner `steps` are **PhaseStep instances only** in Phase 1-8 —
nested `LoopStep` is not supported yet (will be re-evaluated post-Phase
9).

### QualityGate (inside `quality_gates`)

```json
{
  "name":        "<registered gate name>",
  "kind":        "computational" | "inferential",
  "on_fail":     "halt" | "feed_into_next" | "trigger_replan" | "informational",
  "feed_target": "<state.extras key>",
  "config":      {<arbitrary>}
}
```

Required: `name`, `on_fail`. `feed_target` becomes required when
`on_fail = feed_into_next` — invariant enforced at construction.

### CrossStepPolicy (inside `cross`)

```json
{
  "scope":   "global" | "project" | "both" | "skip",
  "handler": "<cross-level function name>"
}
```

`scope` is required. `handler` is dispatch metadata used by the cross
runner to look up a cross-level function (`cross_plan`,
`cross_validate_plan`); it does **not** rename `PhaseStep.phase`. The
semantic phase name is preserved so loop predicates like
`until: validate_plan.approved` continue to evaluate correctly.

**Handler is required when `scope` is `global` or `both`** — these
scopes dispatch the step at the cross level and need an entry in the
cross handler registry. **Handler is optional (and ignored) for
`scope=project` and `scope=skip`** — those steps run inside the child
sub-pipeline (or not at all) where the regular phase handler resolves
by `PhaseStep.phase`. Unknown handler names are rejected at projection
time so authoring bugs surface immediately.

Known handlers (frozen set, see
`pipeline.cross_project.profile_projection.KNOWN_CROSS_HANDLERS`):
`cross_plan`, `cross_validate_plan`. **`contract_check` is not a valid
handler** — it is the cross runner's terminal gate and is invoked
automatically after project pipelines finish. Declaring a step phased
`contract_check` in any profile is rejected at projection time.

A `cross` annotation has no effect on single-project runs — the
mono runner ignores the field. Profiles intended for `orcho cross`
must annotate every step; the cross projector raises if any step
lacks a policy. See `docs/architecture/cross_project_pipeline.md` for
the projection rules and handoff contract.

### HumanReview (inside `human_review`)

```json
{
  "timing":       "before" | "after",
  "actions":      ["approve", "halt", "retry", "reprompt", "edit", "skip"],
  "prompt":       "<custom prompt>",
  "retry_budget": <int>
}
```

Default actions cover APPROVE / HALT / RETRY / REPROMPT. EDIT only
valid with `timing=after`. At least one terminal action is required.

## Until predicates

`LoopStep.until` reads `state.phase_log[<phase>][<field>]`:

- Truthy → exit loop (e.g. `"validate_plan.approved"` exits when
  validate_plan approves).
- `"not <phase>.<field>"` → exit when the read is falsy
  (e.g. `"not review.has_issues"`).

Common convention:
- `<qa_phase>.approved` for QA gates.
- `<review_phase>.clean` for empty-critique signals.

## Shipped profiles (nine semantic work kinds + two internal)

The built-in catalogue is keyed by the semantic work kinds. Each carries
`semantic_profile` (== the key), a deterministic `default_mode`, and a
`recipe_kind`; all ship as `kind: custom` (no `variant`).

| Work kind | `recipe_kind` | `default_mode` | Use |
|-----------|---------------|----------------|-----|
| `feature` | full_cycle | `fast` | plan ↔ validate_plan loop + subtask_dag implement + review_changes ↔ repair_changes loop + final_acceptance; **fresh-run default** |
| `small_task` | full_cycle | `fast` | plan → implement → final_acceptance, no QA loops; cheapest |
| `complex_feature` | full_cycle | `pro` | feature recipe + compliance_check (core ships a no-op stub; plugins register a real audit handler) |
| `planning` | focused | `pro` | produce a plan artifact, halt after validate_plan |
| `research` | focused | `fast` | plan-only recipe reused for exploratory research |
| `delivery_audit` | focused | `pro` | review_changes pass over the current diff + final_acceptance |
| `code_review` | focused | `pro` | review recipe reused for a code-review pass |
| `refactor` | full_cycle | `pro` | feature recipe reused for restructuring work |
| `migration` | full_cycle | `pro` | complex_feature recipe reused for a migration |
| `task` *(internal)* | internal | — | implement against an existing plan (skips planning); hidden from the picker |
| `correction` *(internal)* | internal | — | system follow-up after a rejected delivery (ADR 0085); hidden from the picker |

See [`core/_config/pipeline_profiles_v2.json`](../../core/_config/pipeline_profiles_v2.json)
for the literal text, and
[semantic_profiles_alignment.md](../architecture/semantic_profiles_alignment.md)
for the recipe-migration mapping and the deterministic default-mode table.
Use `profile="auto-detect"` when the caller wants Orcho to recommend the work
kind and mode; use a concrete semantic profile when the caller already knows the
desired workflow.

## Loading

```python
from pathlib import Path
from pipeline.profiles.loader import load_profiles_v2

profiles = load_profiles_v2(Path("core/_config/pipeline_profiles_v2.json"))
profile = profiles["feature"]
# profile is a Profile dataclass; profile.steps is tuple[PhaseStep | LoopStep, ...]
```

`ProfileLoadError` (subclasses `ValueError`) is raised on malformed
input with a location-shaped message
(`"feature.steps[1].quality_gates[0].on_fail: ..."`).

## Phase 5e-5 status (today)

`run_profile` accepts both shapes:
- Legacy `PipelineProfile` with `phases: tuple[str | LoopStep, ...]` —
  retained only for direct runtime tests / private inline helper
  dispatch; the v1 JSON loader and god-methods were deleted in Phase 5d.
- v2 `Profile` with `steps: tuple[PhaseStep | LoopStep, ...]` — accepted
  end-to-end and used by `run_pipeline`. Top-level `PhaseStep` dispatches
  through the lifecycle FSM. `execution` and `quality_gates` are active;
  `skill` / `human_review` remain later-phase fields.

Custom plugin profiles can author against the v2 shape today and run
them through `run_profile` directly:

```python
from pipeline.profiles.loader import load_profiles_v2
from pipeline.runtime import PipelineState, run_profile
from pipeline.phases.builtin import default_registry

profiles = load_profiles_v2(Path("path/to/profile.json"))
state = PipelineState(task="...", project_dir="/path", plugin=plugin)
run_profile(profiles["my_profile"], state, default_registry())
```

## Remaining wiring

- `skill` and `human_review` parse as schema fields but are wired in
  Phase 7 and Phase 8 respectively.
- `orcho.profiles` entry_points discovery and the public `--profile`
  CLI flag land in Phase 7 / Phase 6; until then built-ins load from
  `core/_config/pipeline_profiles_v2.json` and custom callers can invoke
  `load_profiles_v2(path)` directly.
- HumanReview lifecycle stages (Phase 8).

## See also

- [docs/architecture/overview.md](../architecture/overview.md) — top-level
  mental model
- [docs/adr/0003-two-axis-profiles.md](../adr/0003-two-axis-profiles.md)
  — kind+variant rationale
- [docs/adr/0008-pipelinerun-collapse.md](../adr/0008-pipelinerun-collapse.md)
  — Phase 5b migration plan
- [docs/architecture/verification_contract.md](../architecture/verification_contract.md)
  — how a profile's `quality_gates` relate to verification environments and
  authoritative receipts, plus the proposed `work_mode` / `verification`
  vocabulary (not yet profile-schema fields)
- `pipeline/profiles/loader.py` — implementation
- `tests/unit/test_profile_loader.py` — pinned parser behaviour
