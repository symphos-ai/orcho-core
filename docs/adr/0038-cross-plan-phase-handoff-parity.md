# ADR 0038 — cross_plan phase handoff parity with single-run validate_plan

- **Status:** Accepted.
- **Date:** 2026-05-24
- **Deciders:** project owner
- **Builds on:**
  [ADR 0031](0031-generic-phase-handoff-contract.md) — single-run
  phase handoff lifecycle (`continue` / `retry_feedback` / `halt`),
  [ADR 0035](0035-terminal-status-and-resume-observability.md) —
  halt-observability invariants reused on the cross side,
  [ADR 0037](0037-cross-project-halt-artifacts.md) — cross-parent
  artifact invariants this lifecycle hangs off of.

## Context

ADR 0031 ships the `phase_handoff` lifecycle for single-project runs:
when `validate_plan` rejects across the loop's auto budget, the
pipeline pauses (`meta.status="awaiting_phase_handoff"`), the
operator decides `continue` / `retry_feedback` / `halt` via the
MCP tool `orcho_phase_handoff_decide`, and `orcho_run_resume`
restarts the subprocess with the decision applied. Scenarios B
and C in the post-promote MCP smoke validate this end-to-end.

Cross-project does not have it. Inventory of cross-side controls:

| Control | Mechanism | Source |
|---|---|---|
| `manual_confirm` gate run/skip/abort | `awaiting_gate_decision` + `--decision` overrides + inline TTY prompt | `pipeline/cross_project/gate_decisions.py` |
| `cross_plan` round budget exhaustion | for/else `else:` that warns and proceeds with the rejected plan | `pipeline/cross_project/orchestrator.py` ~line 1821 |
| `cross_plan` non-bypass handoff at projection time | rejected — `_reject_non_bypass_handoff_in_projection` | `pipeline/cross_project/profile_projection.py:286` |

The projection-time rejection's docstring is explicit: "Cross-
project handoff support is a separate runtime slice; silently
dropping the policy would let a cross run sail past validate_plan
rejections without the pause the single-project runner honours."
That slice is this ADR.

The for/else block at the cross_plan loop is the equivalent of the
single-run "budget exhausted, no handoff policy declared" path —
except the comment literally reads "Cross-level phase handoff is
not implemented yet... proceeding with last plan (profile declares
bypass handoff)". Today every cross-pipeline that exhausts plan
rounds silently proceeds with the rejected plan. Operators have no
mid-run intervention; the only way to stop a bad cross plan is to
abort the cross run wholesale and re-launch.

## Decision

Extend the single-run phase_handoff lifecycle to the cross_plan
phase, mirroring the ADR 0031 contract as closely as the cross
orchestrator's structural differences allow:

### 1. Handoff trigger

When the projected profile declares `human_feedback_on_reject` on
the cross-projected `cross_validate_plan` step (the QA gate inside
the cross-plan loop) **and** the loop's auto budget exhausts with
the final round still rejected, the cross orchestrator pauses
instead of falling through the for/else "bypass" path.

`human_feedback_always` is reserved for a follow-up — the cross
loop currently lacks per-round operator injection points, and
extending the cross-plan prompt protocol for per-round feedback is
beyond this slice. Profiles that declare `always` on a cross-
projected step still fail projection until that work lands.

### 2. Payload shape

The cross-side `meta.phase_handoff` payload is byte-identical in
shape to single-run (ADR 0031), so MCP / SDK / UI consumers that
already read it for single-runs work unchanged for cross runs:

```json
{
  "id":                "cross_plan:cross_plan_round:<N>",
  "phase":             "cross_plan",
  "type":              "human_feedback_on_reject",
  "trigger":           "rejected",
  "verdict":           "REJECTED",
  "approved":          false,
  "round_extras_key":  "cross_plan_round",
  "round":             <N>,
  "loop_max_rounds":   <max_rounds>,
  "available_actions": ["continue", "retry_feedback", "halt"],
  "artifacts":         { ... last cross_validate_plan review_dict ... },
  "last_output":       "<truncated last cross plan markdown>"
}
```

`handoff_id` distinguishes cross from single via the phase prefix
(`cross_plan:` vs `validate_plan:`). The `round_extras_key` is
`cross_plan_round` so post-mortem tooling that splits the handoff
trace by source can route off either field without a separate
flag.

### 3. Pause persistence + exit code

Mirroring `_apply_phase_handoff_pause` in single-run:

- `session["status"] = "awaiting_phase_handoff"`
- `session["phase_handoff"] = payload`
- `session["halt_reason"]` left unset — handoff is a pause, not a
  terminal halt (single-run keeps the same invariant)
- `save_cross_session` writes `meta.json`
- `_write_cross_checkpoint` persists the cross checkpoint with
  `phase_handoff_pending = True`
- Metrics snapshot is written so the run dir carries `metrics.json`
  even if the operator chooses `halt` later
- `phase.handoff_requested` event emitted
- `run_cross_pipeline` returns the session immediately; `main()`
  maps `awaiting_phase_handoff` → `sys.exit(4)` alongside the
  existing `awaiting_gate_decision` → 4 case

### 4. Resume + decision application

`orcho_run_resume` spawns the cross subprocess with `--resume
<run_id>`. On resume, the cross orchestrator:

1. Loads the cross checkpoint via `_read_cross_checkpoint`.
2. If `phase_handoff_pending` is set, reads the matching decision
   artifact via `sdk.phase_handoff.load_phase_handoff_decision`
   (this SDK function is handoff-source-agnostic — works for
   `cross_plan:*` IDs without code changes).
3. Branches on `decision.action`:

   | Action | Cross orchestrator behaviour |
   |---|---|
   | `continue` | Accept the last rejected plan on disk (`cross_plan.md`), set `plan_approved=True`, skip the cross_plan loop, proceed to per-project pipelines. |
   | `retry_feedback` | Run **one** extra cross_plan round with the operator's feedback string injected into the replan prompt (using the same `cross_replan_prompt` already used for in-loop retries). Round index is `loop_max_rounds + 1`. If this extra round is also rejected, the cross orchestrator pauses again with `handoff_id="cross_plan:cross_plan_round:<N+1>"`. |
   | `halt` | Set `session["status"]="halted"`, `session["halt_reason"]="phase_handoff_halt"`, finalize via the ADR 0037 helper (writes `meta.halt_reason` + `evidence.json`), return. |

### 5. MCP wire compatibility

`orcho_phase_handoff_decide(run_id, handoff_id, action, feedback?,
note?)` is already source-agnostic — it validates the handoff_id
against the active `meta.phase_handoff.id`, persists the decision
artifact, and flips status on `halt`. Cross handoff IDs flow
through without code changes.

`orcho_run_resume(run_id)` reuses the existing supervisor path;
cross resume CLI argv is already handled (line 3140-ish in
`orchestrator.py:main`).

### 6. Projection relaxation

`_reject_non_bypass_handoff_in_projection` is narrowed: it now
rejects only non-bypass handoff on phases OTHER than `cross_plan`
(and `cross_validate_plan` — the QA half of the cross_plan loop
where the policy is conventionally declared). The error message is
updated to point at this ADR.

### 7. Profile updates

The shipped `advanced` profile gets `human_feedback_on_reject` on
its cross-projected `cross_validate_plan` step. The `lite` profile
stays at `human_bypass` (it has no plan loop). Other profiles are
left untouched until each one is reviewed.

## Out of scope

- **`human_feedback_always` on cross_plan.** Adds per-round operator
  injection — requires extending the cross-plan prompt protocol;
  separate slice.
- **Handoff on `contract_check` rejection.** `contract_check`
  already has the `manual_confirm` mechanism + `--decision`
  overrides; a parallel handoff lifecycle here would duplicate
  control surfaces. Tracked as a follow-up if operator feedback
  asks for it.
- **Handoff on `cross_final_acceptance` rejection.** Today CFA
  rejection is terminal; adding handoff would let an operator
  resurrect a rejected release. Worth its own ADR.
- **Per-child handoffs propagating up.** Each child sub-run has its
  own validate_plan handoff via the single-run mechanism; nothing
  changes there. The cross parent does NOT aggregate child handoffs
  into a parent-level signal.

## Consequences

### Wire-format additions

`meta.phase_handoff` may now carry `phase="cross_plan"` in
addition to the existing `phase="validate_plan"`. Consumers that
key off `phase` to render handoff UI need to know about the new
value; consumers that key off `id` / `available_actions` / fields
shared with single-run see no shape change.

`handoff_id` schema gains the `cross_plan:cross_plan_round:<N>`
form. The colon-separated grammar is unchanged.

`meta.status="awaiting_phase_handoff"` is now reachable from cross
runs.

### Behavioural changes

Cross runs that previously sailed past plan rejection with a
warning now pause for operator decision when the active profile
declares `human_feedback_on_reject`. Profiles that stay on
`human_bypass` keep the current bypass behaviour — the warn fires,
the rejected plan proceeds.

### Migration

None. The new lifecycle is opt-in per profile via the existing
handoff declaration. Existing profiles default to `human_bypass`
on cross-projected steps, so behaviour is unchanged until a
profile is explicitly switched to `human_feedback_on_reject`.

`advanced` is opted in by this slice; runs on other profiles see
no change.
