# ADR 0186 — A gate hook routes the whole failure set, not its first member

- **Status:** Accepted
- **Date:** 2026-08-31
- **Related:** [ADR 0081](0081-verification-contract-scheduling-and-repair-routing.md),
  [ADR 0090](0090-require-gate-no-silent-green.md),
  [ADR 0174](0174-typed-timeout-failure-kind.md),
  [ADR 0176](0176-operator-retry-feedback-reaches-the-agent.md)

## Context

ADR 0081 gave a scheduled gate hook a *set* of required commands and a single
routed consequence. The implementation routed the first blocking disposition it
produced and returned: `run_gate_hook` evaluated one gate, called
`_route_failed_gate`, and — for any non-`None` result — stopped.

Production run `20260831_170837_de791f` (project `lesson-editor`, profile
`feature`, gate set `smoke` = `lint` + `typecheck` + `vitest`, all three
`required`, `after_phase: implement` with `policy: require, action: repair_loop`)
showed what that costs. The durable ledger trail records the whole story:

```
selection after_phase implement lint|typecheck|vitest  selected
execution after_phase implement lint       fail   rerun=false
execution after_phase implement lint       pass   rerun=true
selection before_delivery       lint|typecheck|vitest  selected
execution after_phase implement lint       fail   rerun=true
execution before_delivery       lint       fail
execution after_phase implement typecheck  fail   rerun=false   <- pre-final materializer
execution before_delivery       typecheck  fail
```

At `after_phase(implement)` the hook executed `lint` only. `lint` failed, its
repair loop ran, its recheck passed, and the hook returned `passed` — so
`typecheck` and `vitest` were never executed at that hook at all. `typecheck`'s
failing receipt appears later, produced by the pre-final receipt materializer,
long after the repair loop had closed. Two independent failures followed:

1. **The repair loop repaired one of two red required commands.** `policy:
   require` was satisfied by the first member of the set, and the run walked
   into `review_changes` → `repair_changes` → `final_acceptance` carrying a red
   required receipt.
2. **The operator decision surface showed a strict subset of the blockers.**
   The `before_delivery` hook reused the already-failed pre-final receipts, hit
   `lint` first, and raised handoff `gate:lint:1` whose `findings` named `lint`
   alone. The operator verified the lint fix, chose `continue` on that payload,
   and 18 TypeScript errors reached manual delivery review.

Only the delivery guard caught the escape: red required receipts terminated the
run `commit_delivery_verification_blocked` rather than auto-committing.

Both failures are the same defect. The failure-carrying value was a single
`(entry, receipt, classification)` triple, and both the repair routing and the
handoff construction consumed that one triple.

## Decision

**A gate hook executes every selected gate before it routes anything, and
routes the resulting failure set as one unit.**

1. `run_gate_hook` collects blocking failures across the whole hook firing and
   routes once, at the end. `abort` remains the single early exit: it ends the
   run, so there is no aggregate decision surface left to complete and no reason
   to spend the remaining gates' wall-clock.
2. The failure set is the unit of `policy: require`. The hook reports `passed`
   only when every required command of the hook is green.
3. The repair critique (ADR 0081: *the failed command output is the critique*)
   carries **every** failing command's evidence, and the repair loop's exit
   condition is that **every** pending command rechecks green. Each round
   re-narrows to the commands still red, so a repaired command is not chased
   again.
4. A set enters the repair loop only when **every** member is repairable. One
   agent-unfixable member (`provenance_failure`, `env_failure`, `unverifiable`,
   `timeout`) escalates the whole set — burning repair rounds on the fixable
   half and then showing the operator only that half is the reported failure
   mode in miniature.
5. The gate handoff payload lists every failing command: one `findings` entry
   per command (each naming its `command`), plus `gate_commands` and
   `gate_identities`. The singular `gate_command` / `gate_identity` keep naming
   the primary (first) failure, because waiver identity
   (`pipeline/verification_waiver.py`) and handoff-route classification
   (`pipeline/control/handoff_routing.py`) are single-identity contracts. The
   `handoff_id` keeps its parsed `gate:<command>:<round>` shape.
6. A human `retry_feedback` rechecks **every** identity the handoff blocked on,
   not just the primary — the same escape one layer up.

Rendering the set (critique, findings, short summary, handoff artifacts) is
owned by the focused `pipeline/project/gate_failure_set.py`; deciding what to do
with it and doing it stays in `pipeline/project/gate_repair.py`. A
single-failure set renders byte-identically to the pre-set payloads, apart from
the added per-finding `command`.

## Consequences

- A hook firing now runs its full selected command set even when the first
  command fails. This is more wall-clock in the failing case; it is the cost of
  knowing what is actually red before deciding. Cost-scoped rerun hooks
  (`costs=frozenset({"fast"})`) are unaffected — selection still bounds them.
- `verification_command_receipts/` gains the receipts of commands that a
  first-match hook never executed. The ledger trail becomes a truthful record of
  what the hook checked, which is what made this incident diagnosable in the
  first place.
- The handoff payload is additive: `gate_commands` / `gate_identities` are new
  keys in the untyped `artifacts` bag, and `findings[*].command` is a new key.
  No typed SDK/MCP wire shape changes, and readers that only know the singular
  keys keep working.
- **The delivery guard is untouched.** It remains the last fail-closed line,
  and it is deliberately not weakened by this change: a `continue_with_waiver`
  on an aggregate handoff still waives only the primary command's identity, so
  a second red required receipt still blocks delivery. Waiving a whole set, if
  ever wanted, is a separate decision with its own audit shape.
- The mixed repairable/unfixable set trades some automation for honesty: a
  failure an agent could have fixed now waits for the operator alongside the one
  it could not. The alternative — repair half the set, then escalate the rest —
  is exactly the partial picture this ADR exists to remove.
