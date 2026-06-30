# ADR 0001: Pipeline Architecture Redesign

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** project owner

## Context

Orcho's pipeline started as a fixed sequence:
`PLAN → PLAN_QA → BUILD → REVIEW → FIX → FINAL_QA`. As DAG decomposition,
multi-skill routing, parallel worktrees, and cross-project orchestration
became targets, the architecture exhibited symptoms of premature
flattening:

- **`PipelineMode` enum** (LINEAR / DAG / PLAN / REVIEW / TASK) and the
  `Profile` JSON list of phase names were two parallel mode selectors.
- **DAG was modelled as a profile**, but it is structurally a way to
  **execute** a phase that has multi-task content — not a different
  pipeline.
- **`Loop` semantics** (PLAN ↔ PLAN_QA, REVIEW ↔ FIX) lived as imperative
  Python in `_PipelineRun.run_*_loop` god-methods (~671 LoC), entangled
  with session-shape ceremony, mock plan-file writes, agent routing, and
  checkpoint persistence.
- **Quality gates** (test runs) were ad-hoc callbacks, not first-class.
- **Human review** did not exist as a concept; checkpointing was
  baked into `block_on_qa_reject`.
- **Skills** were mixed with execution policy (provider/model selection
  inside skill frontmatter).

## Decision

Re-architect around four **independent first-class concepts**:

1. **`Profile`** — goal-oriented recipe (`kind` × `variant`):
   - `FULL_CYCLE` (lite / advanced / enterprise) — depth of dev cycle
   - `SCOPED` (plan / review / task) — partial workflow scope
   - `CUSTOM` — plugin-defined
2. **`ExecutionMode`** — per-phase execution strategy
   (`linear` vs `dag`); how a phase actually runs internally.
3. **`LoopStep`** — declarative wrapper around `PhaseStep`s with
   `until` predicate, `max_rounds`, and oscillation halt; replaces
   imperative `run_*_loop` methods.
4. **`QualityGate`** — registered post-phase check + fail policy
   (HALT / FEED_INTO_NEXT / TRIGGER_REPLAN / INFORMATIONAL); kind =
   computational vs inferential (per Fowler's harness analysis).

Plus opt-in cross-cutting:

- **`HumanReview`** — 6 actions × 4 backends (CLI / auto-approve /
  auto-halt / MCP-blocking), backend selected globally (env / CLI flag),
  Profile portable across environments.
- **`Attachment`** — multimodal prompt context (file / image / binary).
- **`Artifact taxonomy`** — internal vs external × ephemeral vs durable;
  external profiles (`none` / `minimal` / `adr` / `docs` / `full`)
  ship project documentation through the same artifact pipeline.

## Drivers

- **Composability:** `PhaseStep.execution = "dag"` lets any phase become
  a multi-task wave executor without inventing a separate "DAG profile".
- **Plugin extensibility:** public entry_points groups and matching runtime
  registries form the extension surface for third-party packages shipping
  additional runtimes, phases, profiles, execution modes, session adapters,
  gates, and skills. The active public groups live in `pyproject.toml`;
  lifecycle registries document later discovery groups where they are wired.
- **Reproducibility:** declarative `Profile` JSON + `SkillBinding`
  checksum + `events.jsonl` = audit-able runs.
- **Cross-project orchestration:** a higher-order
  orchestrator over N `run_pipeline` invocations with artifact-based
  contract validation only works if intra-project pipelines are
  declarative (a god-method orchestrator can't compose).

## Consequences

### Positive

- Single source of truth for pipeline control flow:
  `pipeline/runtime.py:run_profile`. The 5 imperative `run_*` methods
  collapse into ~80 LoC of declarative dispatch.
- Plugin authors target **one** consistent extension surface
  (Strategy + Registry + entry_points across all extension groups).
- Testing simplifies: snapshot tests on session shape, property tests on
  profile JSON, mock-package entry-point tests for plugin discovery.
- Profile authoring without writing Python: a profile author ships
  `profile.json` via `orcho.profiles` entry_points.

### Negative / Costs

- **Phase 5 is the largest refactor in the roadmap.** Mitigation:
  Phase 0.5 side-effect matrix (this audit) inventories every behaviour
  before deletion; snapshot tests catch session-shape drift.
- **Existing tests break:** ~10 unit tests across DAG-internal phase
  handlers, profile loading, and `_PipelineRun` methods need rewriting.
  Acceptable: solo project, no external users, no backcompat ceremony.
- **Documentation debt:** 18 ADRs + per-phase guides + reference docs
  must ship alongside code. Mitigated by phase completion bar
  (skeleton + ADR → code can merge; full guides finalize when surface
  stabilizes).

### Neutral

- No backward compatibility shims (project policy: no backcompat
  ceremony for internal plumbing).
- `TestingConfig` / `plugin.testing` / `PipelineMode` /
  `linear_with_retry` deleted, replaced with new schema.

## Alternatives Considered

### A. Keep the imperative god-methods, add DAG support inline

Rejected: doubles down on the structural flaw. Cross-project
orchestration would require yet another wrapping god-method.

### B. Rust rewrite (à la Forge)

Rejected: orcho is Python-first; ecosystem (Anthropic SDK, MCP, agent
CLI subprocesses) is Python-native. Performance bottleneck is external
agent latency, not Python execution.

### C. Adopt Forge / Claude Code skill format only

Rejected as exclusive choice (we adopt **as compatibility readers** —
see ADR 0010). Forge is a pair-programming CLI, not an autonomous
orchestrator; depending on it as the backend would couple the engine to
a single external tool. Backend neutrality invariant (ADR 0011)
explicit.

## Implementation Roadmap

13 approval-gated phase blocks (0 / 0.5 / 1 / 1.5 / 2 / 3 / 4 / 4.5 /
5 / 6 / 7 / 8 / 8.5) + 5 future milestones (M9 worktree-parallel /
M10 web dashboard / M11 artifact pipeline / M12 MCP re-alignment /
M13 cross-project orchestration). Each phase ships:

- Code + tests green
- ADR for architectural decision (this is ADR 0001)
- Schema/API documented enough for next phase to start
- Full authoring guides finalize after surface stabilizes

Historical workspace execution notes lived in a gitignored local plan. The
durable public trail is this ADR plus the architecture docs and phase plans
under `docs/`.

## References

- Side-effect matrix: historical workspace artifact; public owner-stage
  details now live in `docs/architecture/phase_lifecycle.md`
- Mental model: `docs/architecture/overview.md` (Phase 1)
- ADR 0002: frozen dataclasses + invariants (Phase 1)
- ADR 0003: two-axis profiles (Phase 1)
- ADR 0010: adopt Agent Skills standard (R9, Phase 7)
- ADR 0011: runtime not provider (R10, Phase 7)
