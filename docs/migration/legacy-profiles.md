# Legacy Profile Migration (v1 → v2)

> Phase 5 migration note. The legacy v1 profile shape (flat list of
> phase names) is deprecated; the v2 shape (two-axis kind+variant +
> typed PhaseStep/LoopStep) is the new authoring surface.

## Mapping table

| v1 profile | v2 equivalent | Notes |
|------------|---------------|-------|
| `linear` | `advanced` (kind=full_cycle, variant=advanced) | The flat 6-phase list `[plan, plan_qa, build, review, fix, final_qa]` becomes a Profile with two LoopSteps + a final_qa step. |
| `dag` | `advanced` with `execution: "dag"` on the build step | The DAG composite execution mode (Phase 2) is now declared per step rather than per profile. |
| `lite` | `lite` (kind=full_cycle, variant=lite) | Same name, same shape — three sequential PhaseSteps. |
| `enterprise` | `enterprise` (kind=full_cycle, variant=enterprise) | Same name; v2 shape adds explicit `compliance_check` PhaseStep. |
| `linear_with_retry` (example) | Removed | Was a documentation example; v2's `advanced` covers the use case. |

## What the v2 shape adds

- **Per-step richness:** `role`, `skill`, `effort`, `overrides`,
  `quality_gates`, `human_review` per PhaseStep. v1 profiles couldn't
  express any of this (one default agent per phase across all profiles).
- **Declarative loops:** v1 used the orchestrator's imperative
  `_PipelineRun.run_plan_qa_loop` / `run_review_fix_loop` god-methods;
  v2 wraps the inner steps in `LoopStep` with a typed `until` predicate
  and round counter.
- **Two-axis typology:** v1 was a flat namespace. v2 distinguishes
  full-cycle profiles (lite/advanced/enterprise) from scoped profiles
  (plan/review/task) so `--profile plan` and `--profile advanced` are
  semantically different categories.

## What stays the same

- Profile names: `lite` and `enterprise` stay; `advanced` replaces
  `linear` and absorbs `dag`.
- Six phase handlers: `plan`, `plan_qa`, `build`, `review`, `fix`,
  `final_qa`. Same `orcho.phases` entry_points group.
- Session-shape adapters dispatch on the same phase names.
- Quality gates dispatch on the same gate names.

## Migration path for plugin authors

### If your plugin sets `PluginConfig.pipeline = "linear"`:

→ The v1 `linear` profile name is gone. Use the `advanced` v2 profile
name once Phase 7 re-enables `PluginConfig.pipeline` as a per-project
default selector. During Phase 5d, `PluginConfig.pipeline` is accepted
but intentionally ignored; use CLI `--profile` or `ORCHO_PIPELINE` for
run-level selection.

### If your plugin sets `PluginConfig.pipeline = ["plan", "build", "final_qa"]` (inline list):

→ Inline list profiles were a v1-only convenience. Switch to a named v2
profile such as `lite`, or ship a v2 JSON profile via the Phase 7
profile discovery path.

→ For more elaborate inline lists, ship a v2 JSON profile via
`<project>/.orcho/multiagent/profiles.json` (Phase 7 discovery) and
reference by name.

### If your plugin sets `PluginConfig.pipeline = "dag"`:

→ The dedicated `dag` profile name is removed. v2 expresses the
architectural intent as `advanced` with the build step's
`execution: "dag"`, but Phase 5d still treats that field as schema
intent; active DAG-as-build dispatch is deferred to the Phase 5e/6
lifecycle hardening pass.

## Phase 5d status

The v1 JSON file and loader are removed. `_config/pipeline_profiles_v2.json`
is the active profile catalogue for `run_pipeline`.

## Remaining cleanup

- `PipelineProfile` remains as a private in-process helper for direct
  runtime tests / inline helper dispatch.
- `PluginConfig.pipeline` remains as an accepted but ignored field until
  Phase 7 wires v2 profile discovery.
- `TestingConfig` migration is deferred to Phase 5e/6, when
  `PhaseStep.quality_gates` becomes active.

## See also

- [docs/reference/profile_schema.md](../reference/profile_schema.md)
  — full v2 schema
- [docs/guides/profile_authoring.md](../guides/profile_authoring.md)
  — how to write a v2 profile
