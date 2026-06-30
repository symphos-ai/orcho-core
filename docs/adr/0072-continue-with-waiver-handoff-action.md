# ADR 0072 — `continue_with_waiver` phase-handoff action

- Status: Accepted
- Date: 2026-06-03
- Relates to: ADR 0031 (structured phase handoff: decide ≠ resume,
  exact-payload idempotency, fail-fast trigger discipline), ADR 0042
  (project-pipeline handoff service / resume dispatch), ADR 0066
  (repair-receipt re-review protocol), the review/acceptance gates
  (`review_changes`, `final_acceptance`)
- Extends: ADR 0031. (ADRs are append-only; this records a new action on
  the existing handoff vocabulary rather than editing 0031.)

## Context

On a `rejected` phase-handoff pause the operator had three actions:
`continue`, `retry_feedback`, `halt` (ADR 0031). They cover three
intents:

- `halt` — stop; the rejection stands and the run ends.
- `retry_feedback` — "I disagree / it's fixable; here's guidance" — run
  one more reviewer round.
- `continue` — "ship anyway" — proceed past the rejection without an
  extra round.

`continue` has a real gap. It lets the run proceed, but it records
nothing about *why* the operator overrode a `rejected` verdict, and it
does not tell the **downstream** gates that the override happened. A
`review_changes` rejection that the operator waives at the handoff is
still visible to `final_acceptance`, which re-derives its own verdict
from the same artifacts and re-flags the same findings — so the run that
the operator explicitly chose to ship gets blocked again one gate later,
with no record of the human decision in between. The operator's override
was neither **durable** nor **authoritative**.

What was missing is an override that is *accountable* (requires a stated
operator verdict) and *propagated* (the waived findings are carried
forward so no later gate reopens them).

## Decision

Add a fourth phase-handoff action, `continue_with_waiver`, offered
**only on `rejected` verdicts** (an approved pause has nothing to waive).
It proceeds like `continue` for control flow but records a durable,
authoritative waiver.

1. **Accountable.** `continue_with_waiver` **requires** a non-empty
   `feedback` (the operator verdict). The SDK rejects an empty waiver at
   decide time, and the resume path fail-fasts (`RuntimeError`) if the
   verdict is blank — the same feedback-required treatment
   `retry_feedback` already gets. A waiver with no stated reason is not a
   waiver.

2. **Same control flow as `continue`.** On resume the machine verdict
   stays `rejected`, the loop is stripped, and **no** extra reviewer
   round runs. The action writes the usual
   `state.extras["phase_handoff_override"]` so the loop runner exits
   without rewriting the verdict (which would otherwise re-enter the loop
   forever). It does **not** consume a `retry_feedback`-style round.

3. **Durable.** Resume writes a structured waiver to both
   `run.session["phase_handoff_waiver"]` and
   `state.extras["phase_handoff_waiver"]`. The record carries the
   `handoff_id`, `phase`, operator verdict (`waiver_text`), `note`,
   `decided_at`, the waived `findings`, and the prior reviewer
   `critique`. Because session state is persisted to `meta.json`, the
   waiver survives a fresh-process resume: a hydration seam
   (`pipeline/project/state_setup.py`) rehydrates
   `state.extras["phase_handoff_waiver"]` from `meta` on a cold start, so
   the waiver is present regardless of whether the downstream gate runs
   in the same process or a resumed one.

4. **Authoritative over downstream gates.** The waiver is injected into
   every downstream review gate (`review_changes`, `final_acceptance`) as
   a **code-owned** reconciliation directive: the waived findings are to
   be treated as accepted-known and, absent other independent blockers,
   the gate returns `APPROVED` / `ship_ready`. The reconciliation policy
   text is owned by `pipeline/prompts/contracts.py` and rides as a typed
   `TURN` prompt part with `source="operator"` (not user-editable). This
   preserves the prompt-boundary discipline from ADR 0060: the machine
   contract / reviewer gate stays JSON-only; the operator's runtime data
   travels as a distinct, code-controlled channel rather than being
   spliced into the contract.

5. **Auditable.** The waiver is projected into the evidence bundle as a
   `phase_handoff_waiver` error-kind entry (collector + Markdown render),
   recording the waived `phase`, that the verdict was deliberately left
   `rejected`, the operator verdict, and the count of waived findings.

The action joins the existing vocabulary everywhere it is enumerated: the
`PhaseHandoffAction` enum, the SDK `phase_handoff_decide` Literal and
`next_actions` intent, the decision classifier, the runtime-published
`available_actions` (rejected branches only), the interactive TTY prompt
(`4` / `w` / `waiver`), and the resume dispatcher.

## Consequences

- An operator who chooses to ship past a `rejected` review states why,
  once, and that decision is honored end-to-end: later gates do not
  reopen the waived findings, and the override is recorded in session
  state, `meta.json`, and the evidence bundle.
- `continue` keeps its original meaning (silent, unrecorded override) for
  callers that want it; `continue_with_waiver` is the accountable variant.
  No existing action changes semantics.
- The reviewer gate stays JSON-only. The waiver enters as a code-owned
  operator prompt part, so the boundary between machine contract and
  operator runtime data is preserved.

## Out of scope

- **Granular per-finding waivers.** This waiver is per-handoff: it waives
  the findings active at the pause as a set. Selectively waiving a subset
  while keeping others blocking is deliberately deferred — it needs a
  finding-identity contract the gates do not yet emit.
- **Waiver on `approved` verdicts.** There is nothing to waive on an
  approval; the action is rejected-only by construction.
- **Cross-project handoff — explicit contract exception.** The
  cross-orchestration handoff surface (`cfa_gate`, `handoff_payloads`,
  `planning_loop`) is intentionally **not** given a waiver in this slice.
  Its durable-waiver semantics (an operator verdict injected into
  *downstream review gates*) have no cross_plan / CFA equivalent — there is
  no single downstream review gate to reconcile against. Because
  `available_actions` is the single source of action availability, the
  exclusion is enforced authoritatively at the **producer**:
  `build_cross_plan_handoff_payload` and the CFA producer publish only
  `continue` / `retry_feedback` / `halt`, so the SDK decide gate refuses
  `continue_with_waiver` for a cross_plan / CFA handoff upstream. The
  cross resume dispatchers (`planning_loop._resume`, `cfa_gate`) **reject**
  the fourth value with a loud `RuntimeError` rather than mis-routing it,
  since the shared `HandoffDecisionAction` Literal now carries four values
  and is no longer three-way exhaustive.

  One cross path *does* carry the action through, by design: the ADR 0039
  **child-proxy** resume (`cross_project/app.py`) forwards a non-`halt`
  decision to the *child project's* `phase_handoff_decide`, which validates
  it against the **child's** `available_actions`. A child single-project
  review handoff legitimately publishes `continue_with_waiver`, so a
  child-proxied review waiver works end-to-end through the normal
  single-project resume — no cross-specific waiver logic is involved.

  Granular cross-project waiver (a cross_plan-level durable waiver) is
  deferred; it would need a cross-level reconciliation target this design
  does not define.

## MCP alignment (mandatory companion slice)

`continue_with_waiver` is a wire-format change to the handoff action
vocabulary (`available_actions`, the `phase_handoff_decide` Literal, the
decision artifact shape). Per the MCP-validation rule, this
ships with a matching `orcho-mcp` update and an E2E mock smoke in the same
change — the MCP `orcho_phase_handoff_decide` surface must accept the new
action and require its feedback, and the smoke must drive a rejected
review to ship via the waiver without reopening the waived findings.
