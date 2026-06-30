# ADR 0065: Policy-Derived Acceptance — criterion schema + Review Acceptance

- **Status:** Accepted (design locked; implementation in P2)
- **Date:** 2026-06-01
- **Phase:** P0 (terminology + contract only — no runtime change)
- **Companion:** ADR 0064 (semantic profiles & operating modes),
  `docs/architecture/automation_principle.md`
- **Implements:** the verification gate-and-waiver design (phase P2;
  internal planning record, not shipped with this repo)

## Context

`ship_ready` is currently whatever the reviewer LLM writes about itself, and
`plan.commands_to_run` is parsed/counted/rendered/injected but **never executed
or bound** to the verdict. Acceptance criteria float as prose, disconnected from
the commands that would prove them. This lets a run reach final acceptance with a
red gate, and makes the reviewer's binary verdict the sole gate.

This ADR locks two contracts so P1/P2 implement them without re-deciding names.

## Decision 1 — acceptance criteria own their proof

`acceptance_criteria` and `commands_to_run` are **not** two parallel lists. Each
criterion declares **how it is verified**; `commands_to_run` becomes a derived
projection.

```jsonc
"acceptance_criteria": [
  { "id": "c1", "intent": "lint clean",
    "verify": "executable", "tier": "inner",   "check": "ruff check ." },
  { "id": "c2", "intent": "unit suite green",
    "verify": "executable", "tier": "release", "check": "pytest -q -m 'not e2e'" },
  { "id": "c3", "intent": "round-1 behaviour unchanged",
    "verify": "agent_assertion" },
  { "id": "c4", "intent": "UX flow ok",
    "verify": "human" }
]
```

Verification taxonomy (the axis is *who verifies*, not "auto vs manual"):

| `verify` | verified by | relation to verdict |
| --- | --- | --- |
| `executable` | machine (gate runs `check`) | hard veto: red → `ship_ready=false` |
| `agent_assertion` | the agent's own claim | tracked obligation; counted, never proves |
| `human` | a person | rare, explicit; loud + recorded, like a waiver |

Field rules:
- `verify ∈ {executable, agent_assertion, human}` (required).
- `executable` ⇒ `check` (one command or a small list) **and** `tier ∈ {inner, release}`.
- `agent_assertion` / `human` ⇒ no `check`.
- `commands_to_run` (top-level) = de-duplicated projection over `executable`
  checks; if both present they must be consistent (parse-time coherence check).
- Same shape applies to per-task `done_criteria`.

`agent_assertion` names the thing honestly — a *claim*, not a proof — making
self-certification a first-class, auditable metric. A run whose `ship_ready=true`
rests mostly on `agent_assertion` rather than `executable` is a visible yellow
flag.

## Decision 1b — planned subtasks are delivery units

When ADR 0064 resolves `implementation_execution=subtask_dag`, the parsed plan's
required subtasks become delivery accounting units. The implement phase must
produce an implementation receipt for each required subtask with one terminal
state:

- `done` — executed; its `done_criteria` are evaluated with the same
  `executable | agent_assertion | human` proof taxonomy as plan-level
  acceptance criteria.
- `blocked` or `failed` — attempted but could not complete; the reason and any
  gate output are recorded.
- `skipped` or `waived` — not executed by explicit policy or human/config
  decision; this is a verification gap, not proof.

The delivery gate reads those receipts. A phase summary, repair summary, or LLM
claim can explain what happened, but cannot by itself prove that every planned
subtask was completed. `whole_plan` remains the compact executor for small work;
`subtask_dag` is the mode that upgrades subtasks from prompt text to tracked
delivery obligations.

The older `dag_result` metadata produced by `PhaseStep.execution="dag"` is not a
delivery receipt unless it is migrated into this receipt contract. Delivery
acceptance must bind to the policy-owned implementation receipts, not to a
parallel legacy execution surface.

## Decision 2 — Review Acceptance (§6a): cleanliness is policy-derived

The reviewer's verdict is **observational**; the gate decision is **computed by
core** from policy, not by the model.

1. **Reviewer verdict is observational** — the model writes `verdict` + findings
   honestly.
2. **Gate cleanliness is policy-derived** — core computes `effective_clean`.
3. **Findings are never discarded** — classified `blocking` / `advisory` by the
   threshold.
4. **Operator overrides preserve the original verdict and findings.**

`review_blocking_min_severity` (from `OperatingModePolicy`, ADR 0064) = the
minimum severity that blocks (`P3` strictest, blocks everything; `none` =
reviewer purely advisory). Applies to every `review_json` gate:
`review_changes`, `cross_validate_plan`, `contract_check`,
`cross_final_acceptance`.

**Code-shape (do not collapse names):** keep `parsed.approved` = model verdict
(evidence-only). Add runtime `effective_clean` / `gate_clean` +
`blocking_findings` / `advisory_findings`. With `clean = parsed.approved`,
`effective_clean = not blocking_findings`.

Worked example (mode `team`, threshold `P2`): reviewer writes `REJECTED` + a `P3`
finding → runtime marks `P3` advisory → `effective_clean=true` → handoff does not
fire → evidence records "approved by policy; advisory P3" with the raw verdict
preserved. The repair-loop churn on cosmetic findings disappears.

## Combined verdict

```
ship_ready = (all executable criteria PROVEN)
             AND (reviewer effective_clean per review_blocking_min_severity)
```

Gates execute exactly once (inner per round, release at loop exit); final
acceptance is read-only over the loop-exit gate report — it never re-runs gates.

## Consequences

- The verdict stops being the LLM's self-report; it is a function of executed
  checks + policy-classified findings.
- `executable / agent_assertion / human` tally and the blocking/advisory split
  are recorded in evidence (explainability).
- Binding to LLM-authored `check` commands is not circular: execution is the
  deterministic oracle regardless of author; `validate_plan` (P2 / T3b) guards
  against vacuous always-green checks.
