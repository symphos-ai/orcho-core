# Orcho Architecture Overview

> Top-level mental model. Start here, then descend into the concept docs
> only when you need implementation or reference detail.

## Scheduled verification gates

Verification scheduling is core-owned through a durable per-run ledger. Its
snapshot, identity-scoped events, evidence bundle, terminal summary, and SDK
retain the same `(command, hook, phase)` rows. Resume uses the snapshot rather
than resolving a changed plugin. Core wire delivery remains stopped pending the
paired `scheduled-gates-4` `orcho-mcp` L1–L4 validation.

## What orcho is

**Autonomous multi-agent pipeline orchestrator.** Given a task and a
project, orcho runs a configurable pipeline of phases (`plan` →
`validate_plan` → `implement` → `review_changes` ⇄ `repair_changes` →
`final_acceptance`, plus profile variants), each backed by an LLM-powered
agent. Output: working code in the project's git tree, ADR documents,
deliverables manifest, audit-able event log.

orcho is **not** a pair-programming CLI (like Claude Code, Cursor, or
Forge). Those are interactive co-pilots; orcho is a fire-and-forget
orchestrator with opt-in human review checkpoints.

orcho is also not a general agent graph library. Frameworks such as
LangChain or AutoGen help authors build agent workflows; orcho ships an
opinionated software-delivery workflow with planning, implementation,
review, repair, final acceptance, evidence, retries, checkpoints, and
profile-controlled runtime selection already wired together. Use orcho
when the job is "run this development pipeline repeatably and leave an
audit trail," not when the job is "assemble arbitrary agents from
primitives."

## Four first-class concepts

| Concept       | Question it answers                                  |
|---------------|------------------------------------------------------|
| **Profile**   | What pipeline shape achieves this goal?              |
| **PhaseStep** | What this single step does (phase + role + skill)    |
| **LoopStep**  | When does this PhaseStep retry?                      |
| **ExecutionMode** | How does this phase actually run internally?     |

Plus opt-in cross-cutting:

- **QualityGate** — registered post-phase check + fail policy
- **HumanReview** — blocking interactive checkpoint (6 actions × 4 backends)
- **Attachment** — multimodal prompt context (file / image / binary)
- **Skill** — portable instructions package (Agent Skills SKILL.md)

## Two-axis profile typology

```
Profile { name, kind, variant, description, steps }

  kind:                    variant:
    FULL_CYCLE   ─────────┬── lite
                          ├── advanced
                          └── enterprise

    SCOPED       ─────────┬── plan
                          ├── review
                          └── task

    CUSTOM       ─────────  (plugin-defined, variant arbitrary)
```

The profile namespace is flat: every name resolves through the same
loader as `orcho run --profile <name>`; the legacy single-project `--mode`
shim was removed. The shipped catalog (`core/_config/pipeline_profiles_v2.json`)
carries nine operator-facing work-kind profiles — `small_task`, `feature`,
`complex_feature`, `refactor`, `migration`, `planning`, `research`,
`delivery_audit`, `code_review` — plus the internal `task` and `correction`
profiles. `kind` / `variant` above are schema axes on the Profile object,
not catalog names.

These shipped flat profiles are **not** the target semantic profiles. ADR 0064
keeps an accepted `SemanticProfile × OperatingMode → resolver → RunShape`
target whose resolver and `RunShape` are not built yet; the flat profiles stay
the executable shortcuts in the meantime. See
[Semantic profiles — current-state alignment](semantic_profiles_alignment.md)
for the transitional mapping (shipped flat profiles vs target semantic
profiles) and today's live policy knobs.

## Three independent run axes

The semantic profile answers *what kind of work* a run is. Two further,
orthogonal axes answer *how many repositories it spans* and *where its diff may
land* — modelled as closed enums distinct from `SemanticProfile`
([ADR 0102](../adr/0102-run-topology-and-delivery-scope-axes.md)):

- **Run topology** (`RunTopology`: `mono` / `cross_recommended`). A
  deterministic, provider-neutral heuristic (`topology_detection.py`,
  substring-matching the task against a workspace-overridable signal table)
  *recommends* a cross-project run when the work likely spans repositories. It
  is advisory only: a `cross_recommended` run never changes the resolved
  profile, never starts cross, and never widens delivery on its own. The
  operator's choice is explicit and typed; a non-interactive run merely records
  the recommendation in durable `meta.auto_detect`.
- **Delivery scope** (`DeliveryScope`: `strict_mono` / `expanded_mono` /
  `cross`). Enforced at final delivery by collecting changes across *all*
  recommended projects — each alias resolved to a repo path through the
  workspace config, each sibling repo's dirty files gathered and disclosed per
  alias. `expanded_mono` discloses sibling changes and delivers;
  `strict_mono` parks a typed, reversible `delivery_scope_violation`
  blocker (never a crash); a run with no recorded scope delivers exactly as
  before. The same fields and the per-alias blocker/disclosure are projected
  onto the MCP wire.

## How it fits together

```
┌──────────────────────────────────────────────────────────────────┐
│  Cross CLI leaf  (orcho-cross / orcho cross)                     │
│  └─ run_cross_pipeline(task=..., projects=..., ...)  ← 23-kwarg  │
│      └─ CrossRunRequest.from_kwargs(...)  ← back-compat shim     │
│                                                                  │
│  Library callers of cross (MCP cross bridge, future web)         │
│  └─ run_cross_project_pipeline(CrossRunRequest(...))             │
│      ├─ presentation=TERMINAL  → legacy cross transcript         │
│      └─ presentation=SILENT    → zero stdout/stderr; meta.json   │
│                                  + events.jsonl + progress.log   │
│                                  + run.start / run.end + mirror  │
│                                  all byte-identical to TERMINAL  │
└──────────────────────┬───────────────────────────────────────────┘
                       │ per-alias dispatch — every child runs under
                       │ ProjectRunRequest(presentation=SILENT,
                       │                   no_interactive=True)
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  Project CLI leaf  (orcho-run / orcho run)                       │
│  └─ run_pipeline(task=..., profile_name=..., ...)  ← 28-kwarg    │
│      └─ ProjectRunRequest.from_kwargs(...)  ← back-compat shim   │
│                                                                  │
│  Library callers (cross-project per-alias child, direct-library  │
│   UI, MCP)                                                       │
│  └─ run_project_pipeline(ProjectRunRequest(...))                 │
│      ├─ presentation=TERMINAL  → byte-identical CLI transcript   │
│      └─ presentation=SILENT    → zero stdout/stderr; events.jsonl│
│                                  + progress.log + session.json   │
│                                  byte-identical to TERMINAL      │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                 ┌───────────────┴────────────────┐
                 │  ProfileExecutor (run_profile) │
                 │  walks Profile.steps in order  │
                 └───────────────┬────────────────┘
                           │
                ┌──────────┴──────────┐
                │                     │
        per PhaseStep                LoopStep
                │            (per round, until predicate)
                │                     │
                └──────────┬──────────┘
                           │
            ┌──────────────┴──────────────┐
            │  ExecutionMode dispatcher   │
            │  (linear | plugin modes)    │
            └──────────────┬──────────────┘
                           │
            ┌──────────────┴──────────────┐
            │  IAgentRuntime              │ ← orcho.agent_runtimes
            │  (claude / codex / gemini;  │   entry_points
            │   mock = internal test kit) │
            └─────────────────────────────┘
```

## Why these concepts exist

The architecture is split this way so each decision has one owner:

- **Project application boundary** (`run_project_pipeline` +
  `ProjectRunRequest` + `ProjectRunResult`) owns the typed entry into a
  per-project run. The CLI keeps its 28-kwarg `run_pipeline` wrapper for
  back-compat; library callers (cross-project, future direct-library
  UI, MCP if it ever moves off subprocess spawning) build a typed
  request and read a typed result. The boundary is locked by a
  signature-pin test so drift is caught at import time. See
  [ADR 0042](../adr/0042-project-pipeline-application-boundary.md).
- **Presentation policy** (`PresentationPolicy.{TERMINAL, SILENT}` on
  `ProjectRunRequest` and `CrossRunRequest`) owns whether the run is
  allowed to write to stdout/stderr. `TERMINAL` (the CLI / SDK default)
  preserves the legacy transcript byte-identical; `SILENT` produces
  zero stdout/stderr while keeping every persisted artifact
  (`session.json` / `meta.json`, `events.jsonl`, `progress.log`,
  checkpoint, worktree teardown, mirror) byte-identical to `TERMINAL`.
  `SILENT` is hard-paired with `no_interactive=True` at request
  construction. The enum lives in the neutral
  `pipeline.presentation` module so both per-project and cross-project
  boundaries import the same identity. Cross-project's per-alias
  dispatch runs under `SILENT`. See
  [ADR 0046](../adr/0046-silent-app-level-boundary.md) (project
  boundary) and
  [ADR 0047](../adr/0047-cross-project-application-boundary.md) (cross
  boundary).
- **Cross-project application boundary** (`run_cross_project_pipeline`
  + `CrossRunRequest` + `CrossRunResult`) owns the typed entry into a
  cross-project run. Mirrors the project boundary one-for-one: the CLI
  keeps its 23-kwarg `run_cross_pipeline` wrapper for back-compat;
  library callers (MCP cross bridge, future web cross-run path) build
  a typed `CrossRunRequest` and read a typed `CrossRunResult`. The
  signature lock pins both the legacy wrapper and the typed boundary.
  Per-alias dispatch builds a `ProjectRunRequest(presentation=SILENT,
  no_interactive=True)` per child so child banners stay out of the
  parent transcript. Finalization is split into a silent service
  (`finalize_cross_run`) that owns status decision + `run.end` emit +
  persistence + mirror, and a terminal wrapper
  (`finalize_cross_with_terminal_output`) that renders the
  DONE / FAILED banner + chips off the structured
  `CrossFinalizationResult` — the wrapper does not re-decide status or
  re-emit `run.end`. See
  [ADR 0047](../adr/0047-cross-project-application-boundary.md).
- **Profile** owns the pipeline shape: whether a run is lightweight,
  review-only, a full cycle, or a plugin-defined custom flow.
- **PhaseStep** owns the semantic step: plan, implement, review,
  repair, final acceptance, and their configured role/model/prompt/gate
  policy.
- **ExecutionMode** owns how a step runs internally: one handler call
  today, DAG/subtask execution for build-style phases, and plugin modes
  later.
- **PromptPart / PromptTurn** model the prompt as typed data rather than
  one opaque string: a `PromptTurn` is an ordered stream of segments (each
  wrapping a `PromptPart`), and wire text, cache envelope, delta subset, and
  debug transcript are all projections of it (ADR 0060). This lets Orcho
  protect parser/safety contracts, preserve cacheable prefixes, and record
  what each invocation actually sent.
- **Evidence surfaces** own observability: run artifacts, events,
  prompt-render traces, context pressure, and gate results are durable
  enough for CLI, MCP, Web, and later analysis to agree on what happened.
  The `evidence.json` bundle is a lower-bound contract — a fixed set of
  always-present keys that grows only by *adding* optional top-level
  sections, never by bumping `EVIDENCE_SCHEMA_VERSION`. `handoff_advice`
  is one such additive section (alongside `verification_receipts` /
  `verification_readiness`): a per-call + summary digest of the
  handoff-advice retries with classified outcomes (resolved / repeated /
  stopped / unknown) and observe-only usage, present only when a run
  actually invoked the advisor (ADR 0093). `command_stalled` is another
  additive error-kind: a durable record of a stalled command, covering
  both the terminal idle-timeout escalation and the live, non-terminal
  unsafe-process-polling risk flag, each carrying a bounded recovery verb
  set (interrupt / resume / halt). The non-terminal record is the durable
  mirror of a diagnostic that is also observable *while the phase is still
  running* via a write-through event, so status and next-actions can surface
  it before the phase ends; killing a stalled command stays bounded to the
  run's own child process group with a single trigger (idle-timeout), and the
  engine never matches host processes by free-text name (ADR 0103).

This split keeps the runner small: profile authors change workflow
shape, phase plugins change behavior, runtime adapters change model
backend, and prompt authors change editable prose without silently
overriding parser contracts or orchestration policy.

## What this redesign changes

The current architecture replaced the earlier `PipelineMode` enum +
flat list of phase names + imperative `_PipelineRun.run_*_loop`
god-methods with explicit dispatch objects:

- Profile / PhaseStep / LoopStep / ExecutionMode are independent
- Loops are declarative, not imperative
- Quality gates and human review are first-class
- entry_points groups form the plugin extension surface
- Cross-project orchestration becomes possible (Milestone 13)

## Prompt engine and observability stack

Orcho ships a structured prompt-engine layer that owns how every
agent invocation gets composed and what evidence each call leaves
behind:

The prompt engine exists because agent prompts are both policy and
payload. Parser schemas, handoff policy, review-target rules, and
language posture must not be editable project prose; role/task/format
wording should be overridable. Typed `PromptPart` composition makes that
boundary explicit.

A prompt is not a string with metadata kept in sync alongside it — it is
a single canonical `PromptTurn`: an ordered stream of segments, each
wrapping one `PromptPart`. The wire text, the cache/session envelope, the
delta subset sent on a resumed session, and the debug transcript are all
*projections* of that one turn, never separately maintained copies.
Runtime prompt builders return a `PromptTurn`; `str` appears only at the
`IAgentRuntime.invoke` boundary via `turn.text`. Post-builder edits
(prefix, codemap, hypothesis context) go through `PromptTurnEditor`, never
raw string concatenation. See
[Prompt Engine](prompt_engine.md) for the working model and
[ADR 0060](../adr/0060-prompt-turn-canonical-render-surface.md) for the
design record.

The cache-first wire layout exists because provider prefix caching only
works for a contiguous leading byte-identical run. A dynamic artifact or
task body before stable contracts makes later stable text uncached, so
Orcho sorts parts by `cache_scope` (GLOBAL → WORKSPACE → PROJECT →
SESSION → NONE) and then by kind to keep the longest stable prefix
first.

The context-lifecycle evidence exists because shorter prompts and cache
hits do not answer whether a long-running agent is approaching its
active context limit. Every invocation stamps `prompt_render`,
`context_growth`, `context_clearing`, `context_pressure`, and, when the
runtime reports it, `runtime_compaction` into `session.json`; CLI live
and debug modes are projections of the same data.

See [observability_surfaces.md](observability_surfaces.md) for the
single-page navigator that ties the surfaces, the taxonomy layer,
the context source hierarchy, and the CLI modes together.

## Newer control axes (pointers)

Axes added after the sections above were written; each is documented at its
own home rather than re-narrated here:

- **Session disposition** — per-phase `session_continuity` policy
  (`fresh_only` / `loop_continue` / `same_zone_continue`) decides
  continue-vs-fresh before any prompt delta selection
  ([ADR 0113](../adr/0113-session-disposition-policy-and-context-baggage-guard.md);
  [Prompt Engine](prompt_engine.md)).
- **Event-sourced run state** — `events.jsonl` is truth, `meta.json` a
  cache; terminal writers, consistency repair, and the ADR 0115
  terminal-outcome reducer live in `pipeline/run_state/`
  ([Run state](run_state.md)).
- **Verification receipts** — the declared proof contract that can block
  delivery (`require`-policy gates; ADR 0090/0117 family)
  ([Verification contract](verification_contract.md)).
- **Delivery branch policy and providers** — `approve` never auto-commits
  onto the default branch; publishing intent is durable
  (ADR 0119 / [ADR 0121](../adr/0121-git-provider-delivery-publishing.md),
  `orcho.delivery_providers` entry-point group).
- **Unattended runs** — CLI `--no-interactive` auto-continues advisory
  handoffs and turns authoritative ones into typed halts
  ([ADR 0120](../adr/0120-unattended-no-interactive-phase-handoffs.md)).
- **Correction follow-ups** — a rejected release triages into a typed
  correction profile (`correction_triage` phase, ADR 0085/0111).

## Where next

- [Type reference](../reference/types.md) — full type inventory
- [Cross-project pipeline](cross_project_pipeline.md) — single vs cross run
  topology and event identity
- [Phase lifecycle](phase_lifecycle.md) — FSM stages, gates, adapters,
  checkpointing, and metrics
- [Execution modes](execution_modes.md) — the per-step execution registry
  (`linear` + plugin modes) and the policy-owned subtask delivery
- [Session shape](session_shape.md) — `phase_log` to `session.json`
  promotion
- [Quality gates](quality_gates.md) — gate result shape and fail policy
- [Run state](run_state.md) — recorded state, terminal writers, repair
- [Verification contract](verification_contract.md) — declared proof,
  receipts, and the delivery gate
- [Sandbox](sandbox.md) — launch-layer hygiene and the isolation envelope
- [Project documentation strategy](project_documentation_strategy.md) — docs
  as durable delivery memory, with indexing as a later layer
- [Observability surfaces](observability_surfaces.md) — what Orcho
  records about each agent call and where it lives
