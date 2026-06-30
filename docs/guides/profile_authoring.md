# Authoring a Pipeline Profile

> Full authoring guide is still stabilising.
> <!-- TODO(orcho-phase-X): expand with end-to-end pip plugin example
> + override conflict diagnostics. -->

A profile is a goal-oriented recipe — what pipeline shape achieves the user's
intent. The product surface is the set of nine **semantic work kinds** Orcho
ships built-in:

- **Common:** `feature`, `small_task`, `complex_feature`
- **Focused:** `planning`, `delivery_audit`, `code_review`, `research`,
  `refactor`, `migration`

plus two **internal** profiles (`task`, `correction`) that the fresh-run picker
hides. Customer plugins ship their own work kinds through `orcho.profiles`
entry_points.

> **Work kind + default mode.** Each built-in work kind declares an explicit
> `semantic_profile`, a `recipe_kind`, and a deterministic `default_mode`. A run
> uses that default mode unless the operator overrides it with
> `orcho run --mode {fast,pro,governed}` (or an explicit project/contract
> `work_mode`). `governed` is opt-in only — it is never a built-in default.
> `orcho run --profile auto-detect` asks core to recommend a concrete work kind
> and mode from the task text/context; interactive runs can confirm or override
> the recommendation, while non-interactive runs follow the configured
> confidence/fallback policy. See
> [Semantic profiles — current-state alignment](../architecture/semantic_profiles_alignment.md)
> and [ADR 0064](../adr/0064-semantic-profiles-and-operating-modes.md).

## When to write one

- **Domain-specific dev cycle**: Unity projects need an EditMode-tests gate
  before PlayMode tests; PHP projects might need `composer install` before
  tests; etc. A custom profile chains the right gates per phase.
- **Compliance-driven workflows**: regulated shops add their own audit gates;
  the shipped `complex_feature` / `migration` work kinds run a
  `compliance_check` stub that plugins replace with a real audit handler.
- **Team conventions**: a frontend team's review profile might check
  accessibility + bundle size + visual regression in parallel — custom
  inferential gates wired into a review-shaped recipe.

## Quick start

Copy a built-in profile from
[`core/_config/pipeline_profiles_v2.json`](../../core/_config/pipeline_profiles_v2.json),
adapt it to your domain, and save under
`<project>/.orcho/multiagent/profiles.json` (auto-discovery is wired with the
`orcho.profiles` entry_points work). Plugin authors can also load the JSON via
`pipeline.profiles.loader.load_profiles_v2(path)` directly.

For a custom profile, set `kind: custom` and — if you want it to read as a
specific work kind in tooling — declare `semantic_profile` / `default_mode` /
`recipe_kind`. The legacy `kind` × `variant` typology stays available for
plugins, but `variant` is no longer the source of semantic identity.

## Schema reference

See [docs/reference/profile_schema.md](../reference/profile_schema.md) for the
full schema (top-level shape, the semantic identity fields, PhaseStep /
LoopStep / QualityGate / HumanReview, the `kind` × `variant` typology, and
`until` predicates).

## Example: Unity project with EditMode + PlayMode test gates

```json
{
  "unity_feature": {
    "kind":             "custom",
    "semantic_profile": "feature",
    "default_mode":     "fast",
    "recipe_kind":      "full_cycle",
    "description": "Unity dev cycle with EditMode + PlayMode separation",
    "steps": [
      {"loop": {
         "steps": [
           {"phase": "plan",          "execution": "linear",
            "skill": "unity-team-lead"},
           {"phase": "validate_plan", "execution": "linear"}
         ],
         "until": "validate_plan.approved",
         "max_rounds": 2
      }},
      {"phase": "implement", "execution": "linear",
       "skill": "unity-team-lead",
       "quality_gates": [
         {"name": "tests_editmode", "kind": "computational",
          "on_fail": "feed_into_next", "feed_target": "last_test_output"},
         {"name": "tests_playmode", "kind": "computational",
          "on_fail": "halt"}
       ]
      },
      {"loop": {
         "steps": [
           {"phase": "review_changes", "execution": "linear"},
           {"phase": "repair_changes", "execution": "linear"}
         ],
         "until": "review_changes.clean", "max_rounds": 1
      }},
      {"phase": "final_acceptance", "execution": "linear"}
    ]
  }
}
```

`tests_editmode` and `tests_playmode` would be registered as separate custom
gates via `orcho.quality_gates` entry_points — see
[`docs/guides/quality_gate_authoring.md`](quality_gate_authoring.md).

## Implement subtask delivery

Implement subtask delivery is **not** requested via `PhaseStep.execution` (that
field only takes `linear` or a plugin-registered mode; unknown modes are
rejected). It is the profile-level `implementation_execution` policy: use
`whole_plan` for the one-invoke implement behaviour and `subtask_dag` when
`ParsedPlan.subtasks` should become tracked delivery units with receipts. The
shipped `feature` and `refactor` work kinds select `subtask_dag`; the first
implementation is sequential and surfaces `concurrency=1` in implement metadata.

Under `subtask_dag`, a subtask's `done_criteria` become a delivery contract: the
developer must append a typed `subtask_attestation` self-attestation, and Orcho
gates on its shape + completeness (ADR
[0068](../adr/0068-subtask-done-criteria-attestation.md)). A subtask whose
attestation is missing / not-all-met is `incomplete` and blocks delivery — so
write `done_criteria` that are concrete and individually attestable.

## Session split vs. session continuity

The `execution` object carries two **orthogonal** session axes you set
independently (see the schema reference for the full value tables):

- `session_split` (`stateless` / `per_phase` / `per_role` / `common`) — *how* a
  session is shared **across phases** in one pass (the physical-session key
  scope).
- `session_continuity` (`fresh_only` / `loop_continue` / `same_zone_continue`,
  ADR 0113) — *whether* a phase resumes **its own** prior session on a repeat
  invocation / loop round.

They do not constrain each other: `{"session_split": "common",
"session_continuity": "loop_continue"}` is valid and means "share one session
across phases, and resume the prior plan-loop session on round 2+". The shipped
built-in defaults are `loop_continue` for `plan` / `validate_plan` (resume on
round 2+), `same_zone_continue` for `implement` / `repair_changes` (resume only
for a same-write-zone edit follow-on), and `fresh_only` for `review_changes`
(always fresh + compact handoff). Auxiliary invocations (companion / contract
re-emit / audit) are always fresh by their invocation shape, not by a profile
field. If you author the object form for a phase, declare `session_continuity`
explicitly — a profile step that omits it is rejected rather than silently
defaulting to fresh, which would re-introduce the ADR 0113 plan/validate
regression.

## See also

- [`docs/reference/profile_schema.md`](../reference/profile_schema.md) — full
  JSON schema and the semantic identity fields.
- [ADR 0113](../adr/0113-session-disposition-policy-and-context-baggage-guard.md)
  — the session-continuity policy and its declarative per-phase field.
- [`docs/architecture/semantic_profiles_alignment.md`](../architecture/semantic_profiles_alignment.md)
  — the live work-kind surface, default-mode projection, and recipe migration.
- [ADR 0064](../adr/0064-semantic-profiles-and-operating-modes.md) — accepted
  semantic-profile / operating-mode target architecture.
