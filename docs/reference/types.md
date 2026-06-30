# Phase 1 Type Reference

> Skeleton reference. <!-- TODO(orcho-phase-7): expand with full
> field-by-field tables and example JSON for each type once schema
> stabilizes after Phase 7 entry_points work. -->

The **redesign type system** lives across the runtime, lifecycle,
skills, artifact, and cross-project modules. Phase 5e-5 made the v2
Profile shape and StepOutcome FSM the active runtime input. Legacy
`PipelineProfile` remains only for private/direct runtime tests; the
customer-facing `TestingConfig` field was removed and test-gate config
now lives under `PluginConfig.quality_gates["tests"]`.

## Core (`pipeline/runtime/__init__.py`)

### StrEnums

| Enum | Values | Used by |
|------|--------|---------|
| `ExecutionMode` | linear | PhaseStep.execution (open string at type level; only `linear` is built-in, plugins may register more) |
| `ImplementationExecution` | whole_plan, subtask_dag | Profile/AppConfig implement delivery policy |
| `SubtaskReceipt.state` (value set, not a StrEnum) | done, incomplete, failed, skipped | terminal per-subtask delivery state (ADR 0067 + 0068); `incomplete` = exec ok but the done-criteria attestation gate did not close |
| `AgentRole` | architect, developer, reviewer | legacy behaviour-intent types; not runtime selection |
| `EffortLevel` | low, medium, high | PhaseStep.effort |
| `GateKind` | computational, inferential | QualityGate / ContractValidation |
| `FailStrategy` | halt, feed_into_next, trigger_replan, informational | QualityGate / ContractValidation |
| `ReviewTiming` | before, after | HumanReview |
| `HumanAction` | approve, halt, retry, reprompt, edit, skip | HumanReview |
| `AttachmentKind` | text, image, binary | Attachment |
| `SemanticProfile` | small_task, feature, complex_feature, planning, delivery_audit, code_review, research, refactor, migration | Operator-facing work kind |
| `OperatingMode` | fast, pro, governed | Verification/policy strictness; `governed` is explicit opt-in |
| `ProfileKind` | full_cycle, scoped, custom | Profile recipe family; built-ins expose semantic profiles instead of this enum |
| `FullCycleDepth` | lite, advanced, enterprise | Legacy/internal `Profile.variant` vocabulary, not the fresh-run picker |
| `ScopedTarget` | plan, review, task | Legacy/internal `Profile.variant` vocabulary, not the fresh-run picker |
| `ChangeHandoffMode` | uncommitted, commit, commit_set | Profile.change_handoff / AppConfig.pipeline.change_handoff |

### Frozen dataclasses

- `QualityGate(name, on_fail, kind, feed_target?, config?)`
- `HumanReview(timing, actions, prompt?, retry_budget=5)`
  - Invariant: at least one terminal action {APPROVE, HALT, SKIP}
  - Invariant: EDIT only valid when timing=AFTER
- `Attachment(kind, name, content_path? xor content_b64?, mime_type?, ...)`
  - Invariant: exactly one of content_path / content_b64
  - Invariant: mime_type required for IMAGE / BINARY
- `PhaseStep(phase, execution, skill?, effort?, overrides?, prompt?, quality_gates, human_review?)`
- `LoopStep(steps: tuple[PhaseStep, ...], until, max_rounds, round_extras_key, oscillation_halt_after?)`
  - **R2 clean break:** legacy `inner_phases: tuple[str, ...]` replaced
    by `steps`. Backwards-compat property `inner_phases` returns
    `tuple(s.phase for s in steps)`.
- `Profile(name, kind, variant?, description, steps, change_handoff?, implementation_execution?, semantic_profile?, default_mode?, recipe_kind?)`
  - Built-ins are keyed by semantic work kind (`feature`, `small_task`, ...)
    and carry `semantic_profile`, `default_mode`, and `recipe_kind`.
  - `profile="auto-detect"` is a selector token: it resolves to a concrete
    semantic profile before normal profile dispatch.
  - Legacy/internal compatibility: kind=FULL_CYCLE → variant ∈ FullCycleDepth
  - Legacy/internal compatibility: kind=SCOPED → variant ∈ ScopedTarget
  - `change_handoff` is optional; omitted profiles use
    `AppConfig.pipeline.change_handoff`
  - `implementation_execution` is optional; omitted profiles use
    `AppConfig.pipeline.implementation_execution`; shipped `feature` and
    `refactor` select `subtask_dag`

## Skills (`pipeline/skills/types.py`)

R9 model — Agent Skills open standard.

- `SkillPackage(name, description, root_dir, skill_md_path, body, frontmatter, resources, source, checksum, resource_manifest)`
- `SkillBinding(skill_name, activation, source, checksum, phase?, subtask_id?)`
  - `activation ∈ {explicit, architect_selected, user_requested}`
- `SkillResourceBinding(skill_name, relative_path, sha256, size_bytes, ...)`
  - Recorded only when agent actually loads a resource
- `ResourceManifestEntry(relative_path, size_bytes, mtime_ns)`
- `SkillTrustPolicy(trust_packages, trust_user, trust_workspace,
   trust_project, trust_compat_claude, trust_compat_forge)`
  - Project + compat skills OFF by default (autonomous-run security)

## Artifacts (`pipeline/artifacts/types.py`)

Phase 1 ships the full artifact taxonomy. Milestone 11 wires the
generator pipeline + ProjectRepoLock + auto_commit; types stand
ready for plugin authors today.

- `ArtifactKind` StrEnum: internal_ephemeral / internal_durable /
  external_ephemeral / external_durable (audience × persistence)
- `ArtifactProfile` StrEnum: none / minimal / adr / docs / full
- `ArtifactSpec(name, kind, output_path_template, generator, config?,
   commit_message_template?)`
- `ArtifactsConfig(profile, overrides, output_root?, auto_commit, auto_push)`
  - Invariant: auto_push requires auto_commit=True
- `ArtifactRecord(name, path, sha256, size_bytes, generator_used,
   generation_time_s, success, error?, cost_usd?)`

NB: the legacy str-based `pipeline.plugins.ArtifactsConfig` keeps
serving the existing `PluginConfig.artifacts` field; Milestone 11
swaps it for the typed version above.

## Cross-project (`pipeline/cross_project/types.py`)

Types only — Milestone 13 implements the orchestrator.

- `ProjectStatus` StrEnum: pending / running / succeeded / failed / blocked
- `WhenPolicy` StrEnum: all_succeeded / all_finished / any_finished
- `BlockedPolicy` StrEnum: skip / fail / halt
- `ProjectRunRef(alias, run_id, project_dir, artifact_index, status, failed_phase?)`
- `ArtifactSelector(project_alias, artifact_name, optional)`
- `ProjectStep(alias, project_dir, profile, task_template, depends_on, overrides?)`
  - alias matches `/^[a-z][a-z0-9_]*$/`
  - overrides keys whitelisted to {model, effort, dry_run, max_rounds}
- `ContractValidation(name, kind, on_fail, inputs, fires_after, when, on_blocked, config?)`
  - Inputs are `ArtifactSelector`s (R5 — artifacts not sessions)
- `CrossPlanStep(skill?, role, prompt_template?, quality_gates, human_review?)`
- `ContractResult(contract_name, passed, output, duration_s, kind, when_satisfied, cost_usd?)`
- `CrossProjectProfile(name, description, projects, contracts, planning?, parallelism)`
  - **N6:** rejects duplicate canonical project_dir aliases unless
    chained via depends_on (prevents repo write races)

## Lifecycle (`pipeline/lifecycle.py`)

Active in Phase 5e-5 for every v2 `Profile` dispatch.

- `StepStatus` StrEnum: completed / skipped / retry_requested / halted / failed
- `StepOutcome(status, state, reason?, retry_payload?)`
  - Invariant: HALTED / FAILED / SKIPPED require `reason`
  - Invariant: RETRY_REQUESTED requires `retry_payload`
- `LifecycleContext(...)`
  - Carries phase/session/gate/execution registries, provider,
    run_config, helper Protocols, and metrics/checkpoint callbacks.
- `ExecutionModeRegistry`
  - Built-ins: `linear`
- `LinearPhaseStepExecutor`

## Agent runtime registry (`agents/registry.py`)

Active since Phase 7a:

- `AgentRegistry` stores one runtime registry:
  `name -> Callable[[model, effort], agent]`
- `orcho.agent_runtimes` is the only agent-runtime entry_points group.
- `orcho.providers.architect`, `orcho.providers.developer`, and
  `orcho.providers.reviewer` are deleted; there is no aliasing layer.
- `AgentRegistry.resolve(model, runtime, effort=...)` is the low-level
  construction API.
- `AgentProvider.resolve(runtime, model, effort=...)` is the
  orchestration-facing API. The positional order is intentionally
  different; call-sites must not transpose it.
- `AgentRegistry.architect()` / `.developer()` / `.reviewer()` remain as
  compatibility wrappers.

Runtime selection is per phase through
`AppConfig.phase_runtime_map[phase]` / `phase_model_map[phase]`, backed
by `_config/config.defaults.json`, `_config/config.local.json`, and env
vars such as `RUNTIME_REVIEW_CHANGES` / `MODEL_REVIEW_CHANGES`. Implement
subtask delivery is selected by
`OperatingModePolicy.implementation_execution=subtask_dag` /
`pipeline.implementation_execution` (see ADR 0067), not a profile-step
execution mode. Subtask model selection remains `SubTask.model` first, then the
BUILD phase model.

Built-ins are `claude`, `codex` and `gemini`. New runtimes register
under the same entry-point group and must implement `IAgentRuntime`.
Runtime constructors are side-effect free; external CLI binaries are
resolved lazily on first `invoke()`.

Deferred follow-up:

- Phase-level per-step runtime override outside DAG subtask execution
  would require `PhaseStepExecutor` to rebuild `PhaseAgentConfig` per
  step.

## SubTask extensions (`agents/entities.py`)

Additive in Phase 1; consumed by Milestone 9 / 11.

- `owned_files: tuple[str, ...]` — glob patterns; wave executor uses
  for proactive conflict avoidance
- `architectural_decision: bool` — flag for ADR generator emit
  (Milestone 11)

## PluginConfig extensions (`pipeline/plugins.py`)

Additive in Phase 1; consumed by later phases.

- `quality_gates: dict[str, dict[str, Any]]` — active config source for
  the built-in `tests` quality gate
- `skill_trust: SkillTrustPolicy` — Phase 7 trust gate

## PipelineState extensions (`pipeline/runtime/__init__.py`)

- `attachments: tuple` — Phase 4.5 wires CLI / MCP loaders +
  per-runtime multimodal translation
