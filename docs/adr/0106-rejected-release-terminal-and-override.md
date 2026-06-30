# ADR 0106 — Rejected final-acceptance terminal semantics and observable override

Status: Accepted

## Context

A single-project run closes with the `final_acceptance` release gate (ADR
0025). The gate's verdict is advisory to the *handler*: a `REJECTED` release is
**not** a parser/schema hard halt, and the handler does not stop the run
(ADR 0022 — quality-gate handlers stay non-halting; the operator owns the
decision). Historically that left a real hole at the *terminal*:

- A run whose release was `REJECTED` (verdict `REJECTED`, `ship_ready=false`,
  or a non-empty `release_blockers` list) but whose delivery never applied the
  diff finalized as a clean `status="done"`. The terminal banner printed
  "Release: rejected -> delivery blocked", yet `meta.status` said `done`,
  `meta.halt_reason` was null, and `meta.commit_delivery` was **absent** — the
  auto/non-interactive delivery resolver returned a `not_applicable` decision
  that the producer dropped without persisting.
- Downstream consumers keying off `status` / `halt_reason` (SDK resume-gate,
  `compute_next_actions`, MCP wire, dashboards) saw a successful terminal with
  no pending decision: a rejected release was indistinguishable from a clean
  ship. The only "no-diff" rejected sub-case (`final_acceptance_no_diff`) was
  handled specially; the general rejected-with-diff case was not.

There is also a legitimate **operator override**: at a TTY the delivery dialog
(ADR 0069) lets an operator ship a rejected change anyway (it is their
checkout, their call). That override must reach `done`, but it must never look
like a clean success — the rejection has to stay durably visible.

## Decision

A run whose `final_acceptance` rejected the release ends in an **actionable
non-success terminal**, with exactly one exception — an operator override that
actually applied delivery — which ends `done` **only** with a durable,
observable rejection+override marker. A rejected release is never a silent
successful `done`.

### 1. Terminal status (finalization)

`pipeline/project/finalization.py` generalizes the no-diff reject precedent. The
source of truth is the persisted `final_acceptance` (or
`cross_final_acceptance`) record. A release reads as **rejected** when any of:

- `verdict == "REJECTED"`, OR
- `ship_ready is False`, OR
- `approved is False`, OR
- a **non-empty `release_blockers`** list.

The `release_blockers` clause is explicit defense-in-depth on top of the schema
invariant in `core/contracts/release_schema.py` (an `APPROVED` verdict forbids
blockers — `validate_release_dict` raises `ReleaseSchemaError`). Even if a
future writer leaked a blockers-only record past verdict/ship_ready, the
presence of blockers alone still drives the rejected terminal.

After `_run_commit_delivery`, a focused helper
(`_apply_rejected_release_terminal_outcome`, modeled on the no-diff helper)
acts only on a still-`done` session:

- **Delivery not applied** (no operator override) — flip `done` to `halted`
  with `halt_reason="final_acceptance_rejected"` and record a structured
  `rejected_outcome` (reason, `status="halted"`, `release_verdict`,
  `release_blockers`, short summary, human-readable message).
- **Delivery actually applied** (`committed` / `applied_uncommitted` =
  operator override) — keep `done` but record a durable `delivery_override`
  marker (`release_verdict`, `release_blockers`, `delivery_status`, override
  reason) so the outcome is observably distinct from a clean success.

The pre-existing no-diff reject path keeps its own, more specific
`halt_reason="final_acceptance_no_diff"`: it settles its non-`done` terminal
first, so the general helper's `done`-guard leaves it untouched. The approved
clean path and the planning/research short-circuit are likewise untouched.

### 2. Persisting the rejected decision (producer)

`pipeline/project/run.py` no longer drops the rejected delivery decision. The
post-delivery guard persists a `not_applicable` decision that represents a
rejected release (non-empty, non-`APPROVED` `release_verdict`) into
`meta.commit_delivery`, while still dropping true non-delivery cases
(`disabled`, `no_diff`, and `not_applicable` with an empty/`APPROVED` verdict).
The decision carries `release_verdict`, `release_summary`, and the structured
`release_blockers` list. When no authored `short_summary` exists,
`pipeline/engine/commit_delivery.py` still folds blocker titles into
`release_summary` as a readable fallback, but the durable blocker list remains
the source of truth for operator UI and SDK projections.

### 3. SDK visibility

`sdk/run_control/delivery.py` treats the persisted rejected-release
`not_applicable` decision as a **decidable correction gate**, not "no pending
delivery gate": `delivery_decision_state` returns `decidable=True`,
`kind="correction"`, with a `reason` referencing the verdict, summary, and
blocker ids/titles;
`decide_delivery` refuses a shipping action with the typed `release_blocked`
blocker (ADR 0099 deferred-delivery decision surface).

For a rejected release the gate blocks **`approve` / `apply` *and* `skip`**,
leaving only `fix` (correct + re-review) and `halt` (give up, stays
recoverable) available. `skip` is refused because it does not apply delivery
yet settles to `skipped`, which is a done-status: allowing it would call
`mark_run_done`, clear the `final_acceptance_rejected` halt, and present the
rejected run as a clean `done` with no `delivery_override` marker — the exact
silent-success hole this ADR closes. The consequence is that **no SDK action
can move a rejected release to `done`**: the only path to `done` is an
actually-applied operator override through the TTY delivery dialog (ADR 0069),
which always records the durable override marker (§1). For an `APPROVED`
release whose delivery is parked on a verification or delivery-scope block,
`skip` stays available — the run was a success, so skipping delivery is a
legitimate clean `done`.

`sdk/actions.py::compute_next_actions` already returns a non-empty actionable
recovery for the `final_acceptance_rejected` halt (a `halted` run with no
failure record projects an `orcho_run_resume` action); a clean terminal-success
still returns `()`. The `delivery_override` marker and the rejected
`commit_delivery` ride through `RunMeta.extra` / `RunStatus.raw_meta`
unchanged, so an override-`done` run is observably distinct from a clean
success on the status surface.

## Consequences

- A rejected release is now an actionable `halted` terminal that stays
  resumable/decidable; the blockers and rejection reason are visible in
  `meta`, evidence, and the SDK.
- `meta.commit_delivery` is preserved for the rejected decision and carries
  `release_verdict` / `release_summary` / `release_blockers` (summary fallback
  folded in only when no short summary was authored).
- Operator override is the single path from a rejected release to `done`, and
  it is always tagged with a durable `delivery_override` marker. In the
  non-interactive/auto path that override is unreachable (auto hard-blocks the
  rejected release and the SDK gate refuses shipping actions), so the
  override-`done` marker is exercised at unit level; the actionable-halted
  terminal is exercised end-to-end by the acceptance mock flow.
- No new command/action shape is required — the surfaces reuse the existing
  `CommitDeliveryDecision`, `DeliveryDecisionState`/`DeliveryDecisionResult`,
  `RunStatus`, and `Action` shapes while preserving the already-defined release
  blocker contract in `meta.commit_delivery`.

## Related

- ADR 0022 — phase taxonomy and the `block_on_plan_reject` narrowing
  (blocking-on-reject fires for `validate_plan` only; a `final_acceptance`
  rejection does not itself stop the run, which is exactly the hole the
  terminal semantics here close).
- ADR 0025 — release gate and cross final acceptance (verdict shape and the
  `release_blockers` schema this builds on).
- ADR 0069 — commit/delivery dialog (the operator override path).
- ADR 0099 — deferred-delivery decision service (the decidable gate the SDK
  surface reuses).

## Amendment — rejected-terminal marker carries the engine-backstop reason

Status: Accepted (append-only amendment).

### Context

The original decision built the `rejected_outcome` / `delivery_override`
markers from only two fields off the `final_acceptance` record: the agent's
parsed `release_blockers` and the agent's `short_summary`. That was correct
when the agent itself rejected the release. It was misleading when the verdict
was **forced by an engine backstop** rather than the agent.

When the engine receipt backstop (ADR 0090 — require-gate, no silent green)
overrides a positive agent answer to `verdict="REJECTED"`, the forced cause is
written into `verification_gaps` (and an `engine_backstop` `{reason, gaps}`
block), **not** into `release_blockers` — which stays equal to the agent's
parsed blockers (empty when the agent said ship-ready). The terminal builder
read neither field, so an engine-forced REJECT surfaced to the operator and the
phase-handoff artifact as "empty `release_blockers` + positive agent summary":
a run that reads as ship-ready yet halted as rejected. An empty
`release_blockers` was, in effect, the only signal on the REJECTED terminal.
This was operator-found on the dogfood run of 2026-06-29.

### Decision

The rejected-terminal builder now carries the **authoritative engine-backstop
reason** on both rejected branches. The finalization seam
(`_apply_rejected_release_terminal_outcome`) reads `verification_gaps` and
`engine_backstop` off the **same** `final_acceptance` record the handler wrote
— no re-derive, no re-classification — normalizes them with
`terminal_outcome.normalize_engine_reason` into a small typed
`EngineBackstopReason`, and passes it to
`resolve_rejected_release_terminal`. The reducer owns the marker shape:

- When the engine reason is present, the marker (both `rejected_outcome` and
  the `delivery_override` sibling) gains an `engine_backstop` (`{reason, gaps}`)
  and/or `verification_gaps` field naming the cause, and the marker `message`
  is led by the engine cause as headline.
- The agent's positive `short_summary` is no longer presented as the headline
  of a REJECTED terminal: when an engine reason is present the summary is
  explicitly tagged `(superseded agent view)` with the engine verdict above it
  in `message`.
- The agent parse contract is unchanged: `release_blockers` in the marker stays
  the agent's blockers (ADR 0025 release schema). Empty `release_blockers` is
  no longer the sole signal on a REJECTED terminal.

### Parity

When no engine reason is recorded — the pure agent-blocker REJECT (non-empty
`release_blockers`, no backstop) and the operator-override-without-backstop case
— the markers stay **byte-identical** to the pre-amendment form: no new keys,
verbatim `short_summary`. The APPROVED supersede branch is untouched.

### Notes

This is a standalone observability fix that is independent of the
scope-expansion sanction (ADR 0112-D). Once that sanction is promoted the
scope-expansion backstop is gone, but the required-receipt backstop (ADR 0090)
is permanent, so the reproduction and the regression test use the
**required-receipt** engine backstop, not scope-expansion, and survive D.

### Related (amendment)

- ADR 0090 — require-gate, no silent green (the engine receipt backstop that
  forces the REJECT whose reason this marker now surfaces).
- ADR 0112-D — scope-expansion sanction policy (out of scope here; this fix is
  reproduced through the required-receipt backstop so it survives D).
