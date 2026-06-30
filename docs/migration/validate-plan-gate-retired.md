# Migration: validate_plan gate → generic phase handoff

> **TL;DR:** The validate_plan-specific gate shipped in ADR 0022 was
> retired in the phase-handoff slice (commits `29768ee` …
> `c173726` in orcho-core, `386ff9d` in orcho-mcp, `8f2a613` …
> `a201f6d` in orcho-web, `2257d71` / `856f5e3` in orcho-ui-kit).
> Pause semantics are now a declarative `PhaseStep.handoff` policy on
> the active profile; decisions land via
> `phase_handoff_decide(run_id, handoff_id, action, feedback?, note?)`.
>
> **No backward compat.** Per the orcho `feedback_no_backcompat_ceremony`
> policy (solo project, no installed base). Update direct callers.
> See [ADR 0031](../adr/0031-generic-phase-handoff-contract.md) for
> the full contract.

## Quick mapping table

| Legacy (ADR 0022) | New (ADR 0031) | Note |
|---|---|---|
| `meta.status = "awaiting_plan_approval"` | `meta.status = "awaiting_phase_handoff"` | Generic status; works for any phase whose declared handoff fires. |
| `meta.plan_gate` payload | `meta.phase_handoff` payload | Now the **canonical** active source of truth. |
| `state.validate_plan_gate_blocked` | (gone) | Pause is decided by the loop runner via `state.phase_handoff_request`. |
| `PipelineStatus.AWAITING_PLAN_APPROVAL` | `PipelineStatus.AWAITING_PHASE_HANDOFF` | |
| `EventKind.VALIDATE_PLAN_GATE_BLOCKED` | `EventKind.PHASE_HANDOFF_REQUESTED` | Payload: `phase`, `handoff_type`, `trigger`, `round`, `handoff_id`. |
| `<run_dir>/plan_gate_decision.json` (single) | `<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json` (one per handoff) | Multiple sequential handoffs in the same run coexist as separate files. |
| `sdk.validate_plan_decide(run_id, "approved" | "rejected", note?)` | `sdk.phase_handoff_decide(run_id, handoff_id, action, feedback?, note?)` | `action` ∈ {`continue`, `retry_feedback`, `halt`}. |
| `sdk.load_validate_plan_decision(run_id)` | `sdk.load_phase_handoff_decision(run_id, handoff_id, ...)` | Strict reader; corrupt artifact raises `InvalidPhaseHandoffState`. |
| `sdk.InvalidValidatePlanGateState` | `sdk.InvalidPhaseHandoffState` | |
| MCP `orcho_plan_gate_decide(run_id, decision, note?)` | MCP `orcho_phase_handoff_decide(run_id, handoff_id, action, feedback?, note?)` | Sig mirrors SDK. |
| `--block-on-plan-reject` CLI flag | (gone) | Pause is declarative — pick a profile that declares non-bypass handoff. |
| `--max-plan-rounds` CLI flag | (gone) | Plan-loop budget is `LoopStep.max_rounds` in the active profile. |
| `pipeline.block_on_plan_reject` config key | (gone) | |
| `pipeline.max_plan_rounds` config key | (gone) | |
| `BLOCK_ON_PLAN_REJECT` env var | (gone) | |
| `MAX_PLAN_ROUNDS` env var | (gone) | |

## CLI: `orcho run`

```bash
# Pre-phase-handoff-slice
orcho run --task "…" --project /p \
    --max-plan-rounds 2 \
    --block-on-plan-reject

# Phase-handoff-slice
orcho run --task "…" --project /p \
    --profile advanced       # declares human_feedback_on_reject on validate_plan
```

Pause behaviour is now profile-driven:

| Profile | Pause on `validate_plan` |
|---------|--------------------------|
| `lite` | Never (`human_bypass`) |
| `advanced` / `enterprise` | On final-round rejection (`human_feedback_on_reject`) |
| `plan` | After every round, approved or rejected (`human_feedback_always`) |
| `review`, `task` | n/a (no validate_plan step) |

## CLI: `orcho cross`

Historical Slice 1 note: `orcho cross --profile advanced` (or
`enterprise` / `plan`) originally **fail-fasted at projection time**
with a structured error:

> cross-project phase handoff lands in a later slice. Use `human_bypass`
> on validate_plan or switch to the `lite` profile for cross runs.

That fail-fast was intentional for Slice 1 — see
[ADR 0031 § Cross-project fail-fast is intentional](../adr/0031-generic-phase-handoff-contract.md#4-cross-project-fail-fast-is-intentional).
Silently dropping the policy at cross-projection would have broken
the declarative contract.

Current cross support is narrower and explicit: ADR 0038 supports
`human_feedback_on_reject` for the cross-plan validation loop, and
ADR 0039 proxies child `review_changes` handoffs through the cross
parent. Unsupported non-bypass handoff shapes still fail projection;
the remedy is to switch that projected step to `human_bypass`, choose
a compatible profile such as `lite`, or extend the supported cross
handoff set.

## Python API: `run_pipeline`

```python
# Pre-phase-handoff-slice
from pipeline.project_orchestrator import run_pipeline
run_pipeline(
    task="…",
    project_dir="…",
    profile_name="advanced",
    max_plan_rounds=2,
    block_on_plan_reject=True,
)

# Phase-handoff-slice
from pipeline.project_orchestrator import run_pipeline
run_pipeline(
    task="…",
    project_dir="…",
    profile_name="advanced",  # advanced declares human_feedback_on_reject
)
```

`max_plan_rounds=` and `block_on_plan_reject=` raise `TypeError`.

## Python API: `build_orch_argv`

```python
# Pre-phase-handoff-slice
from pipeline.argv import build_orch_argv
argv = build_orch_argv(
    project="/p", task="…",
    max_plan_rounds=2, block_on_plan_reject=True,
)

# Phase-handoff-slice
argv = build_orch_argv(project="/p", task="…", profile="advanced")
```

## SDK: deciding a paused run

```python
# Pre-phase-handoff-slice
from sdk import validate_plan_decide
validate_plan_decide(run_id, "approved", note="plan addresses concern")
# … then orcho_run_resume(run_id) separately

# Phase-handoff-slice
from sdk import phase_handoff_decide, load_active_phase_handoff
handoff = load_active_phase_handoff(run_id)
phase_handoff_decide(
    run_id,
    handoff_id=handoff.id,     # e.g. "validate_plan:plan_round:2"
    action="continue",         # or "retry_feedback" / "halt"
    note="plan addresses concern",
)
# … then orcho_run_resume(run_id) separately
```

The two-step **decide ≠ resume** split is preserved (and now
documented explicitly — see ADR 0031 § 1).
`retry_feedback` requires a non-empty `feedback` string and runs
exactly one extra human-directed plan round.

### Reading decisions back

```python
# Pre-phase-handoff-slice
from sdk import load_validate_plan_decision
decision = load_validate_plan_decision(run_id)  # single per run

# Phase-handoff-slice
from sdk import load_phase_handoff_decision, load_phase_handoff_decisions
single = load_phase_handoff_decision(run_id, handoff_id)   # strict reader
all_decisions = load_phase_handoff_decisions(run_id)        # lenient bulk
```

Each run can carry **multiple** decision artifacts (one per
`handoff_id`); the legacy `plan_gate_decision.json` singleton model
is gone.

## SDK: idempotency

`phase_handoff_decide` is idempotent **only on exact payload match**
for the same `handoff_id`:

| Replay | Behaviour |
|--------|-----------|
| Same `action` + same `feedback` + same `note` | Success replay. Artifact **not** rewritten, `decided_at` **not** refreshed. |
| Same `action`, different `feedback` / `note` | `InvalidPhaseHandoffState` (conflict). |
| Different `action` | `InvalidPhaseHandoffState` (conflict). |

This is stricter than the legacy `validate_plan_decide` which
refreshed `decided_at` on same-decision replay. The new contract
makes MCP retries / UI double-submits silent no-ops without allowing
the audit text to drift.

`halt` artifacts survive `meta.phase_handoff` clearing — replaying
the exact same `halt` after the run is already `halted` is an
idempotent success. Different payload → conflict.

## MCP: `orcho_run_start`

```python
# Pre-phase-handoff-slice
await orcho_run_start(
    task="…", project_dir="/p", profile="advanced",
    max_plan_rounds=2,
    block_on_plan_reject=True,
)

# Phase-handoff-slice
await orcho_run_start(
    task="…", project_dir="/p", profile="advanced",
)
```

The arguments are gone; pass them and the call raises `TypeError`.

## MCP: `orcho_phase_handoff_decide`

```python
# Pre-phase-handoff-slice
orcho_plan_gate_decide(run_id, "approved", note="…")
# → PlanGateDecideResult(run_id, decision, note, decided_at)

# Phase-handoff-slice
status = orcho_run_status(run_id)
handoff_id = status.meta["phase_handoff"]["id"]
orcho_phase_handoff_decide(
    run_id,
    handoff_id=handoff_id,
    action="continue",          # "continue" / "retry_feedback" / "halt"
    note="…",
)
# → PhaseHandoffDecideResult(run_id, handoff_id, phase, action,
#                            feedback, note, decided_at)
```

`orcho_run_status` exposes the active payload at
`meta.phase_handoff` plus the decided sub-state:

```python
status = orcho_run_status(run_id)
if status.phase_handoff_decided:
    # Decision artifact exists for the active handoff_id; the next
    # transport action is orcho_run_resume, not another decide call.
    decision = status.phase_handoff_decision
    if decision["action"] in ("continue", "retry_feedback"):
        await orcho_run_resume(run_id)
```

## meta.json layout

```diff
 {
   "task": "…",
   "project": "…",
   "model": "…",
   "profile": "advanced",
   "timestamp": "…",
-  "status": "awaiting_plan_approval",
+  "status": "awaiting_phase_handoff",
-  "plan_gate": {
-    "approved": false,
-    "rounds": [ {…} ],
-    "plan_file": "/abs/path/plan.md"
-  },
+  "phase_handoff": {
+    "id": "validate_plan:plan_round:2",
+    "phase": "validate_plan",
+    "type": "human_feedback_on_reject",
+    "trigger": "rejected",
+    "verdict": "REJECTED",
+    "approved": false,
+    "round_extras_key": "plan_round",
+    "round": 2,
+    "loop_max_rounds": 2,
+    "available_actions": ["continue", "retry_feedback", "halt"],
+    "artifacts": {"plan_file": "/abs/path/plan.md"},
+    "last_output": "…critique…"
+  },
   "phases": { … }
 }
```

`meta.phase_handoff` is the **canonical** active payload (ADR 0031 § 3).
`run.session["phase_handoff"]`, if older UI plumbing still reads it,
is a compatibility mirror generated from `meta` — never write to it
directly.

After `halt`: the decision artifact lands first, then `meta.status`
becomes `halted`, then `meta.phase_handoff` stops being treated as
active (pending-queue filters key on `status + active payload`, not
on the stale value). Historical inspection still reads
`phase_handoff_decisions/`.

## Pause budget — global flag → profile

The pre-slice contract had **two** budget knobs at runtime:

| Pre-slice | Phase-handoff slice |
|-----------|---------------------|
| `--max-plan-rounds` (process-wide CLI flag) | `LoopStep.max_rounds` declared in the profile JSON |
| `pipeline.max_plan_rounds` config default | (gone) |
| `MAX_PLAN_ROUNDS` env override | (gone) |

A custom profile that wants a 3-round plan loop with
`human_feedback_on_reject` writes it once in profile JSON:

```jsonc
{
  "kind": "full_cycle",
  "variant": "custom_advanced",
  "steps": [
    {"loop": {
       "steps": [
         {"phase": "plan", "execution": {"mode": "linear"}, …},
         {"phase": "validate_plan",
          "execution": {"mode": "linear"},
          "handoff": {"type": "human_feedback_on_reject"},
          …}
       ],
       "until": "validate_plan.approved",
       "max_rounds": 3,
       "round_extras_key": "plan_round"
    }},
    …
  ]
}
```

`--max-rounds` survives as the runtime cap on the **review/repair**
loop (a per-task iteration budget that is not tied to handoff
semantics). Its semantics didn't change.

## Web dashboard

| Pre-slice | Phase-handoff slice |
|-----------|---------------------|
| Launch form: "Plan rounds" + "Block Build if Plan QA didn't approve" widgets | Removed (profile-driven) |
| Pending banner / sidebar badge: keyed on `awaiting_plan_approval` | Keyed on `awaiting_phase_handoff` |
| Review state: `plan_gate_review` | `phase_handoff_review` |
| Review screen: Approve / Cancel buttons | Continue / retry_feedback / halt / cancel |
| `meta.plan_gate` reads | `meta.phase_handoff` reads (canonical) |

The review screen also exposes the **decided-but-not-resumed**
sub-state: if a decision artifact exists for the active
`handoff_id` (e.g. recorded from MCP / CLI / another tab), the
form switches to a "▶ Resume" button instead of re-prompting.

Resume preserves the parent run's **original** profile (ADR 0031 § 1):
a `plan` profile run that continues stays under `plan`,
`enterprise` stays under `enterprise`. The Web layer no longer
forces `task` / `advanced` on the resume spawn.

## Cancel vs halt

`cancel` and `halt` are different verbs and live on different paths:

| Action | Path | Effect |
|--------|------|--------|
| `cancel` | UI-only (no SDK call) | Writes `meta.status="cancelled"` directly, marks checkpoint cancelled. Use when the operator wants to walk away from the run without recording a handoff decision (e.g. corrupt decision artifact, or the run has gone stale). |
| `halt` | `phase_handoff_decide(action="halt")` | Writes a decision artifact, then synchronously flips `meta.status="halted"` and clears `meta.phase_handoff`. The proper way to end a paused run that you've actually decided on. |

`cancel` works even when the SDK refuses to record a decision (e.g.
corrupt audit artifact), which makes it the safe-exit affordance
for repair flows.

## See also

- [ADR 0031 — Generic Phase Handoff Contract](../adr/0031-generic-phase-handoff-contract.md)
  for the load-bearing invariants
- [ADR 0022 — Phase Taxonomy Cleanup](../adr/0022-phase-taxonomy-cleanup.md)
  for the historical context (the legacy gate is documented there as
  shipped behaviour; ADR 0031 supersedes its public surface bits)
- [Phase lifecycle](../architecture/phase_lifecycle.md) for where
  phase-handoff fits in the per-step FSM
