# ADR 0111 — Typed correction follow-up after a rejected final acceptance

Status: Accepted

## Overview

After a final acceptance is rejected, the only forward motion is a
**`from_run_plan` follow-up that carries the held diff** — never a bare
same-run resume and never an inert repeat of the same `fix` decision. This ADR
makes that direction *typed and consistent across every read surface*:

1. The delivery-decision projection
   (`sdk.run_control.delivery.delivery_decision_state`) for a fix-marked or
   auto-refused rejected gate stops advertising the inert `fix` repeat as an
   actionable next step and instead routes the client to a `from_run_plan`
   follow-up, naming the parent handle and the held `diff.patch`.
2. The resume intent (`pipeline.control.resume_context.get_resume_intent_options`)
   and the run-control service (`sdk.run_control.service.RunService.resume`)
   classify a **bare (task-less) resume** of a `commit_decision_fix` /
   `final_acceptance_rejected` / `final_acceptance_no_diff` parent as
   *meaningless* — checkpoint is blocked and a follow-up task is required.
3. Once the follow-up child actually delivers, finalization **supersedes** the
   parent so it stops reading as an active correction candidate everywhere.

This is an append-only refinement of
[ADR 0106](0106-rejected-release-terminal-and-override.md) (which made a rejected
release write a decidable `fix`-or-`halt` correction terminal) and
[ADR 0109](0109-supersede-stale-final-acceptance-rejection.md) (which reconciled
a *same-run* approved retry). ADR 0111 adds the **direction** of the correction
(toward a `from_run_plan` follow-up) and the **cross-run** supersession that
ADR 0109 did not cover.

## Context

ADR 0106 closed the "silent rejected success" hole: a rejected release with a
real diff and no applied delivery becomes a `halted` terminal
(`halt_reason="final_acceptance_rejected"`, structured `rejected_outcome`), and
the persisted `commit_delivery` reads as a decidable `fix`-or-`halt` correction
gate. ADR 0109 made the *same-run* approved retry evict that residue.

Two gaps remained once the operator actually starts correcting:

- **The correction had no typed next step.** After `delivery_decide('fix')` the
  gate's `status` is `fix_requested`; an auto-refused rejected release dead-ends
  as a `not_applicable` decision still carrying the rejected verdict
  (`_is_rejected_release_gate`). In both states the read projection still offered
  `fix` as the default actionable choice — but repeating `fix` is **inert** (it
  only re-marks the run), and a bare resume cannot advance a hard terminal
  (the ADR 0106 terminal is deliberately non-resumable). The genuinely
  actionable move — re-running the *held plan* as a fresh follow-up
  (`orcho run --from-run-plan <parent>`), which carries the captured
  `diff.patch` forward — was nowhere in the typed surface. Clients (and the
  resume prompt) could loop on an inert `fix` or attempt a no-op resume.

- **A delivered follow-up never closed its parent.** ADR 0109 reconciles a run
  against *its own* later verdict. But the sanctioned correction path produces a
  **distinct** child run (the `from_run_plan` follow-up). When that child
  succeeded, the parent's phantom rejected `commit_delivery` gate, its
  `rejected_outcome`, and its stale `release_blockers` stayed authoritative —
  so `delivery_decision_state(parent)` still read as a decidable correction and
  the parent still showed as an open correction candidate, indefinitely.

The two gaps share a root cause: the rejected-FA terminal modeled "a correction
is needed" but never modeled "the correction is *requested and directed*" or
"the correction is *done elsewhere*".

## Decision

### 1. The delivery projection routes to a `from_run_plan` follow-up

`delivery_decision_state` (`sdk/run_control/delivery.py`) gains a focused early
branch for a correction whose `fix` decision is already taken
(`status == "fix_requested"`) or that dead-ended on an auto-refused rejected
release (`_is_rejected_release_gate`). For that state it returns:

- `decidable = True`, `kind = "correction"` (unchanged domain);
- `available_actions = ("halt",)` — only "give up" remains; the inert `fix`
  repeat and the shipping/`skip` actions are all in `blocked_actions`
  (`("fix", "approve", "apply", "skip")`), preserving every ADR 0106 block;
- `default_action = None` — no in-gate action is the meaningful next step;
- `reason` built by `_followup_correction_reason(run_id, run_dir)`, a
  human-readable pointer naming the follow-up handle
  (`orcho_run_start from_run_plan=<run_id>`) and, when the file is present, the
  held durable `run_dir/diff.patch` (the non-persisted `patch_text` is never
  read).

The freshly defer-parked rejected gate (`status == "pending"` +
`release_blocked`) is **untouched**: there `fix` is still the actionable operator
decision, exactly as ADR 0106 specified. The approved-pending / non-rejected
delivery path is byte/structure-identical — the branch is not reached and no new
field is added to `DeliveryDecisionState` or `DeliveryDecisionResult`.

### 2. A bare resume of a correction terminal is not meaningful

`get_resume_intent_options` (`pipeline/control/resume_context.py`) gains an
optional `requires_followup_task: bool = False` field. For a
`commit_decision_fix` / `final_acceptance_rejected` / `final_acceptance_no_diff`
parent with no new task it returns `can_checkpoint=False`, `can_followup=True`,
`default_mode=FOLLOWUP`, `reason="correction-followup-required"`,
`requires_followup_task=True`. `RunService.resume`
(`sdk/run_control/service.py`) returns
`RunControlUnsupported(reason="correction-followup-required")` for a bare
task-less resume of such a terminal — no inert run is started. The
`is_terminal_*` classifiers are **not** weakened: the ADR 0106 hard terminal
stays a hard terminal; the new behavior is *direction*, not a resumable
checkpoint. Every other intent shape (success / plain halt / incomplete /
awaiting) is byte-identical (the field defaults to `False`).

### 3. A delivered follow-up supersedes its rejected-FA parent

`pipeline/project/finalization.py` gains
`_supersede_parent_correction_after_followup(run)`, the **cross-run** analogue of
ADR 0109's `_supersede_stale_rejection_residue`. It runs from
`finalize_project_run` immediately after `_apply_rejected_release_terminal_outcome`
(inside the `output_dir` block), so it observes the child's post-delivery
session. When **all** of these hold:

- this run is a `from_run_plan` follow-up — its
  `state.extras["plan_source_run_id"]` names a parent run id;
- the child actually delivered —
  `commit_delivery.status ∈ {committed, applied_uncommitted, skipped}`;
- the parent (loaded by id via `sdk.runs.find_run` / `load_meta`, scoped to the
  child's runs dir) is genuinely a rejected-FA / `commit_decision_fix` terminal
  (reusing the T1 `resume_context` classifiers),

it reconciles the **parent** `meta.json`:

1. evicts the phantom rejected `commit_delivery` gate and its
   `multi_project_delivery` mirror — so `delivery_decision_state(parent)` returns
   `decidable=False, kind="none"` and the parent's stale `release_blockers` are
   no longer authoritative;
2. evicts the terminal-rejection residue (`rejected_outcome`, `halt_reason`,
   `halted_at`, `halt`, `delivery_override`);
3. settles the parent to `done` and stamps a durable `superseded_by_followup`
   marker (`{child_run_id, child_status, delivery_status, reason}`).

It is **idempotent** (a re-run sees the already-settled `done` parent — no longer
a rejected/fix terminal — and stops) and **guarded** (a no-op without a valid
`plan_source_run_id`, on an unsuccessful child delivery, or for a non-correction
parent). It is best-effort: any lookup / read / write failure degrades to a no-op
and never breaks the child's own finalization. The same-run approved-retry path
(`_supersede_stale_rejection_residue`) is untouched — the new helper fires only
for a distinct `from_run_plan` child.

### 4. The typed projection is MCP-visible and companion-delivered (T4)

The core changes above express the correction direction through fields the MCP
companion **already maps** — `kind` / `reason` / `available_actions` /
`decidable` on the delivery gate, the resume preflight outcome, and the
reconciled `meta.json` the diagnosis surfaces project off. No new core SDK wire
field is introduced. The typed `from_run_plan` follow-up **action** surfaced in
`orcho_run_diagnose`, `orcho_delivery_gate`, and `orcho_run_live_status` is an
**MCP-visible projection**: FastMCP serializes only the MCP Pydantic models, and
the core→MCP mapping is explicit. Keeping the four surfaces consistent
(`live_status.resume_meaningful=false`, `diagnose` / `delivery_gate` showing the
`from_run_plan` action, `resume` preflight refusing) therefore **requires a
companion change in `orcho-mcp`** that maps these core facts and is covered by an
E2E mock smoke. That companion delivery is the responsibility of T4 in the same
plan; core must land before the MCP projection. The contract of the correction
belongs to the core — the companion only projects core facts; it introduces no
provider-specific policy.

## Consequences

- A rejected-FA / fix-marked correction gate reads, on every surface, as
  "correction requested → run a `from_run_plan` follow-up with the held diff",
  never as an actionable inert `fix` or a meaningful bare resume.
- A bare resume of such a parent is a typed refusal
  (`correction-followup-required`), not an inert run.
- A delivered `from_run_plan` follow-up closes its parent: the parent reads as
  `done` + `superseded_by_followup`, its delivery gate is non-decidable, and its
  old `release_blockers` are no longer authoritative — consistently across the
  delivery gate, diagnose, and live status (all project off the reconciled
  `meta.json`).
- The approved-pending / non-rejected delivery path and the same-run approved
  retry are unchanged: no new SDK wire field, the `DeliveryDecisionState` /
  `DeliveryDecisionResult` serialization for those gates is byte/structure-
  identical, and `_supersede_stale_rejection_residue` does not regress.
- The MCP-visible typed `from_run_plan` action is not automatic: it lands only
  with the T4 companion mapping + E2E smoke in `orcho-mcp`.

## Related

- [ADR 0106 — Rejected final-acceptance terminal semantics and observable override](0106-rejected-release-terminal-and-override.md)
- [ADR 0109 — Supersede a stale rejection terminal and phantom delivery-gate on an approved re-run](0109-supersede-stale-final-acceptance-rejection.md)
