# ADR 0022 — Workflow-semantic phase taxonomy; retire QA/fix jargon

**Status:** Accepted
**Date:** 2026-05-13
**Supersedes:** none
**Companion to:** ADR 0009 (composable prompt parts), A5.2c collapse of
runtime-role layer

## Context

After A5.2c the engine has:

- **One prompt-role/persona taxonomy for prompt rendering:**
  `systems_architect` / `implementation_engineer` / `code_reviewer` /
  `product_owner`. Lives on `PromptSpec.role`.
- **Per-phase runtime/model routing:** `phases.<name>.{provider, model,
  effort}` in `_config/config.{defaults,local}.json` plus
  `PROVIDER_<PHASE>` / `MODEL_<PHASE>` env overrides.
- **Hardcoded phase dispatch:** phase handlers look up agents by
  phase-derived slot names (`plan_agent`, `build_agent`, etc.).

But the phase IDs themselves are still:

```
plan / plan_qa / build / review / fix / fix_escalate / final_qa
```

This vocabulary mixes three smells:

1. **`*_qa` jargon.** "QA" is a team-role label, not a workflow step.
   `plan_qa` reads as "the QA team reviews the plan"; the workflow
   semantic is "validate the plan against acceptance criteria". The
   prompt-persona axis already cleaned similar smell
   (`reviewer` → `code_reviewer`). Phase IDs should do the same.

2. **`fix` overloaded.** `fix` is a generic English verb and a git
   commit prefix. In the phase taxonomy it means "address review
   findings". Grep'ing the codebase for `fix` returns commit
   messages, bug-fix comments, test fixtures, and the phase ID
   indistinguishably. A workflow term should not collide with the
   ambient vocabulary.

3. **`fix_escalate` looks like a phase but isn't.** It is a
   `PhaseAgentConfig` slot for round > 1 of the repair loop —
   different agent/model for the harder retry. It never appears in
   profile JSON as a `phase` field, but it shares the `phase` naming
   convention and shows up next to real phase IDs in
   `_config/config.defaults.json`. Readers reasonably think it's a
   phase.

These issues are surface-only — the runtime, the prompt rendering,
and the routing all work fine — but the names teach the wrong
mental model to anyone reading code, profiles, configs, evidence,
or MCP wire traces.

## Decision

Retire QA/fix vocabulary. Replace phase IDs with workflow-semantic
names in one clean cut, no backcompat aliases.

### Phase ID rename map

| Old | New | Type |
|---|---|---|
| `plan` | `plan` | unchanged |
| `plan_qa` | `validate_plan` | profile phase |
| `build` | `implement` | profile phase |
| `review` | `review_changes` | profile phase |
| `fix` | `repair_changes` | profile phase |
| `final_qa` | `final_acceptance` | profile phase |
| `fix_escalate` | `repair_escalation` | **config-only slot** (not a profile phase) |
| `hypothesis_qa` | `validate_hypothesis` | hypothesis-loop phase |

`compliance_check` and `decompose` already carry workflow semantics
and stay unchanged. `hypothesis` stays unchanged. The `cross_plan`
prompt task stays unchanged.

### Prompt task file renames (`_prompts/tasks/*.md`)

| Old | New | Rationale |
|---|---|---|
| `plan_qa.md` | `validate_plan.md` | 1:1 with `validate_plan` phase |
| `build.md` | `implement.md` | 1:1 with `implement` phase |
| `fix.md` | `repair_changes.md` | 1:1 with `repair_changes` phase |
| `hypothesis_qa.md` | `validate_hypothesis.md` | 1:1 with `validate_hypothesis` phase |
| `code_review.md` | **stays** | used by mid-loop `review_changes` |
| new: `final_acceptance.md` | (new file) | dedicated to `final_acceptance` phase semantics ("ready to ship?") |

`code_review.md` does not collapse to `review_changes.md` because
the persona-agnostic generic-review task semantic still applies
mid-loop. `final_acceptance.md` gets its own file because the final
gate's question is "ready to ship?", not "what should be fixed?" —
the framing matters for the final reviewer's voice.

### Public surface decisions (locked in)

1. **MCP tool rename.** `orcho_qa_decide` → **`orcho_plan_gate_decide`**.
   The tool resolves the plan-validation gate; the new name names
   the gate explicitly and drops "QA" jargon.

2. **`block_on_qa_reject` parameter rename.**
   `orcho_run_start.block_on_qa_reject` and
   `AppConfig.pipeline.block_on_qa_reject` →
   **`block_on_plan_reject`**. Narrower (fires on `validate_plan`
   rejection only), paritets with the tool rename.

3. **CLI flags follow exact phase ID:**
   - `--model-build` → `--model-implement`
   - `--model-fix` → `--model-repair-changes`
   - `--model-review` → `--model-review-changes`
   - `--provider-build` → `--provider-implement`
   - `--provider-fix` → `--provider-repair-changes`
   - `--provider-review` → `--provider-review-changes`
   - new: `--model-validate-plan`, `--provider-validate-plan`,
     `--model-final-acceptance`, `--provider-final-acceptance`,
     `--model-repair-escalation`, `--provider-repair-escalation`
   - `--model-plan` and `--provider-plan` unchanged.

4. **Env vars follow exact phase ID:**
   - `PROVIDER_BUILD` → `PROVIDER_IMPLEMENT`
   - `MODEL_BUILD` → `MODEL_IMPLEMENT`
   - `PROVIDER_FIX` → `PROVIDER_REPAIR_CHANGES`
   - `MODEL_FIX` → `MODEL_REPAIR_CHANGES`
   - `PROVIDER_FIX_ESCALATE` → `PROVIDER_REPAIR_ESCALATION`
   - `MODEL_FIX_ESCALATE` → `MODEL_REPAIR_ESCALATION`
   - `PROVIDER_REVIEW` → `PROVIDER_REVIEW_CHANGES`
   - `MODEL_REVIEW` → `MODEL_REVIEW_CHANGES`
   - `PROVIDER_PLAN_QA` → `PROVIDER_VALIDATE_PLAN`
   - `MODEL_PLAN_QA` → `MODEL_VALIDATE_PLAN`
   - `PROVIDER_FINAL_QA` → `PROVIDER_FINAL_ACCEPTANCE`
   - `MODEL_FINAL_QA` → `MODEL_FINAL_ACCEPTANCE`
   - `PROVIDER_PLAN`, `MODEL_PLAN` unchanged.

### Internal-surface renames (mechanical follow-on)

- `PhaseAgentConfig` fields:
  `plan_qa_agent` → `validate_plan_agent`,
  `build_agent` → `implement_agent`,
  `review_agent` → `review_changes_agent`,
  `fix_agent` → `repair_changes_agent`,
  `fix_escalate_agent` → `repair_escalation_agent`,
  `final_qa_agent` → `final_acceptance_agent`.
  (`plan_agent` unchanged.)
- Session adapters: `PlanQAAdapter` → `ValidatePlanAdapter`,
  `FinalQAAdapter` → `FinalAcceptanceAdapter`. Registry keys flip
  with the phase IDs.
- Phase handlers: `_phase_plan_qa` → `_phase_validate_plan`,
  `_phase_build` → `_phase_implement`, `_phase_review` →
  `_phase_review_changes`, `_phase_fix` → `_phase_repair_changes`,
  `_phase_final_qa` → `_phase_final_acceptance`,
  `_phase_hypothesis_qa` → `_phase_validate_hypothesis`.
- Event kinds:
  `plan_qa.verdict` → `validate_plan.verdict`,
  `plan_qa.gate_blocked` → `validate_plan.gate_blocked`.
  Python enum: `PLAN_QA_VERDICT` → `VALIDATE_PLAN_VERDICT`,
  `PLAN_QA_GATE_BLOCKED` → `VALIDATE_PLAN_GATE_BLOCKED`.
- State attribute: `state.plan_qa_gate_blocked` →
  `state.validate_plan_gate_blocked`.
- Loop predicates: `"plan_qa.approved"` →
  `"validate_plan.approved"`, `"review.clean"` →
  `"review_changes.clean"`.
- Mock knob: `mock_plan_qa_reject` →
  `mock_validate_plan_reject`.

## Consequences

### Wire-format breaks

This is a coordinated break across orcho-core + orcho-mcp.
Clients that read run evidence, observe events, decide on the
plan gate, or pin profile phase names see new vocabulary:

- `orcho_run_evidence(phases=[...])` enum values change.
- `orcho_plan_gate_decide` replaces `orcho_qa_decide`.
- Event kinds in `events.jsonl` and SSE feeds change.
- Profile JSON files written before this change require
  hand-migration: every `phase:` field, every `prompt.task` field
  pointing at a renamed `_prompts/tasks/*.md`, every
  `until: "plan_qa.approved"` / `until: "review.clean"` predicate.
- MCP schema snapshot rotates.

### No backcompat ceremony

Per the workspace "no backcompat ceremony" rule (single-developer
project, no production install base), there are no aliases, no
deprecation period, no dual-path migration. Old CLI flags raise
`argparse` errors; old env vars are silently unset; old profile
JSON keys fail loud at load time.

If an external consumer subscribes to the wire format, this commit
breaks them. None do today.

### Snapshots / fixtures regenerate

`tests/fixtures/golden/*.json` and `docs/mcp_schema.json` are
regenerated as part of the commit and inspected manually before
landing. Diffs must be limited to phase-name token rotation; any
session-shape change outside the rename is a bug.

### Old names live only in history

- ADR history (this file, ADR 0009, ADR 0010) keeps the old names
  in their original context.
- Internal migration planning notes reference the old → new mapping
  for trace-readability.
- No active doc, no `# legacy:` comment in code, no runtime
  configuration tolerates the old names after this commit.

## Out of scope

- **Prompt persona taxonomy** — already settled by ADR 0009 + A5.2c.
- **Per-phase runtime/model routing** — already settled by A5.2c.
- **`AgentRole` enum collapse** — kept as narrow cross-project
  behaviour-intent label per A5.2c; deferred for separate ADR if
  ever needed.
- **Default content of `_prompts/tasks/final_acceptance.md`** —
  ships as a starter file mirroring `code_review.md` semantics but
  with a "ready to ship?" framing. Editorial follow-up is a
  separate task, not gated by this rename.

## Implementation phases

Tracked in the companion phase-taxonomy-cleanup planning record
(internal). Order:

1. ADR + plan (this file + plan doc).
2. orcho-core: config keys, profile JSON, phase handlers, agents,
   session adapters, event kinds, state attributes, env vars, CLI
   flags, prompt task file renames, tests, golden regen.
3. orcho-mcp: tool rename, parameter rename, schema regen, tests.
4. External evaluation bench: doc references only (no case
   directories named after old phases; bench cases are `fix/`,
   `replan/`, `code_review/` — which are case families, not
   phase IDs).
5. Verification: full suite green per repo, `rg "plan_qa|final_qa|
   fix_escalate"` empty across active code/docs.

Single coordinated commit across orcho-core + orcho-mcp;
evaluation-bench doc updates can ride the same coordinated change
or a small follow-up commit.

## Final mental model (landed 2026-05-12)

After the rotation landed across orcho-core (`06aa6eb`) and orcho-mcp
(`3e85148`), the canonical surface is:

**Workflow phases** (linear pipeline order, public IDs):

```
plan → validate_plan → implement → review_changes → repair_changes → final_acceptance
```

**Config / runtime-only slots** (not phases in the workflow sense):

- `repair_escalation` — per-phase runtime/model routing slot, never
  emitted as a workflow step.

**Hypothesis utility** (pre-plan agent invocation):

- `validate_hypothesis` — gate on the hypothesis artifact, parallel
  in shape to `validate_plan` but separate utility.

**MCP wire surface**:

- Tool: `orcho_plan_gate_decide` (was `orcho_qa_decide`).
- Blocking knob: `block_on_plan_reject` (was `block_on_qa_reject`),
  **plan-side only** per Q2 — `final_acceptance` no longer halts on
  soft-fail REJECTED.

**Semantic outcome — the load-bearing one**:

- `final_acceptance` = the final reviewer's verdict / evidence record.
- `validate_plan` = the only blocking plan gate.
- There is no fuzzy "QA" anywhere — not in phase IDs, not in the MCP
  tool name, not in the blocking flag. Each name says what it does.

This is the mental model future contributors should hold; ADRs that
reshape the pipeline downstream must respect this vocabulary or
supersede it explicitly.
