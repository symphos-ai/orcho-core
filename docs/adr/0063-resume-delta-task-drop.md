# ADR 0063 — Resume-delta drops the re-sent task from the wire

- Status: Accepted
- Date: 2026-06-01
- Relates to: ADR 0026 (session-aware prompt parts), ADR 0028 (cache-first
  wire layout), ADR 0030 (runtime context autonomy), ADR 0060 (PromptTurn
  canonical render surface)

## Context

Reviewer/architect phases re-run across rounds (plan↔replan loop,
validate_plan retries). On a **resumed** session (round 2+), the runtime
already holds the original task in its conversation history — it was sent on
round 1 under the same physical session key. Yet the prompt builders re-emit
the full task every round as a `turn_input` part (`kind=turn_input`,
`stability=TURN`, `cache_scope=NONE`). Because that classification is
"always-send" for the session-aware delta selector, the full task went back on the wire
every resumed round: context bloat and token waste, contradicting the ADR 0026
delta intent ("on a resumed session, send only the per-turn delta").

Observed: a plan-loop round-2 replan re-sent the entire ~3.5k-token task on
top of conversation history that already contained it; validate_plan retries
did the same.

Reclassifying the task as a stable part does **not** fix this: the task sits
in the payload region, and the selector's prefix-contiguity defense
(`delta.py`) re-sends already-sent stable payload parts. The omission must be
an explicit, mode-gated drop.

## Decision

A caller may name parts to **drop from the effective delta wire** on a resumed
turn, via `delta_droppable_part_ids`. The drop is a property of the
**selection**, never of the source turn.

1. **Source turn stays canonical.** `turn.envelope()` is always built from the
   full source turn (ADR 0060). `part_ids`, `prefix_hash`, `payload_hash`, and
   context-clearing classification continue to describe the full prompt. The
   task is removed only from `SelectionResult.selected_parts` (the effective
   wire), so `turn.render_selected(selected_parts)` produces a wire without it.

2. **Drop fires only on the resumed-delta path.** `select_parts_for_turn`
   honours `delta_droppable_part_ids` only in `_delta_render`. Full renders
   (stateless / no state / fresh session) ignore it. A new predicate
   `will_render_delta(state, split)` captures the selector's resumed-delta
   condition as the single source of truth; the drop can never disagree with
   `render_mode`. Fail-safe: any doubt (runtime/model change, no committed
   session, `continue_session=False`) → full render → task on the wire.

3. **`sent_part_keys` stays cumulative.** It is a union that only grows. A
   dropped part is excluded from the turn's `selected`, so this turn adds no
   new sent key for it — but any key recorded on a prior round (e.g.
   validate_plan reuses the same `turn_input:validate_plan_task` id every
   round) is preserved. The selector never removes an already-sent key because
   a part was dropped from the current wire.

4. **Observability.** `SelectionResult` gains `delta_dropped_parts` /
   `delta_dropped_part_keys`; the `prompt_render` trace records
   `delta_dropped_part_keys`; the durable trace + evidence schema carry a
   counts-only `delta_dropped_count`. Completeness invariant:
   `part_ids ⊇ selected_part_keys ∪ omitted_part_keys ∪ delta_dropped_part_keys`.

### Scope

In: plan↔replan (`turn_input:replan_task`) and validate_plan retries
(`turn_input:validate_plan_task`, parsed-plan branch only).

Out (follow-up): `review_changes` / `repair_changes`. They share the same
`_session_aware_invoke` path, but repair reuses the implement session
(`phase="implement"`, `trace_phase="repair_changes"`); "already sent across the
implement→repair boundary" needs separate analysis. The generic
`delta_droppable_part_ids` mechanism makes that follow-up a per-call-site
one-liner.

## Compaction residual risk (accepted)

Correctness of the omission rests on the task being in conversation history.
Three layers protect it: (a) it was sent on round 1 into the same physical
session; (b) the `coding_agent_compaction` contract names
`task_and_acceptance` as preserve slot 1, instructing the runtime to keep it
across its own auto-compaction; (c) ADR 0030 contract-recovery does
`reset_session()` → full re-prime when a parseable contract breaks, and a
cleared `session_id` forces a full render (task re-sent).

Residual gap: a runtime that silently auto-compacts, drops the task, **and**
still emits a structurally valid contract — recovery never fires and the task
is gone. **Decision: trust the contract + document.** This is the same trust
implement/repair already place in history. No pressure→reprime safety belt
(forbidden in Phase 1, ADR 0030). If this gap ever bites in practice, the fix
is a compaction-event-gated full re-send, tracked as a follow-up.

## Consequences

- Resumed plan/validate rounds send only the per-round delta (critique / new
  plan); the original task is no longer duplicated on the wire.
- The source/effective split (ADR 0060) is strengthened: the drop is the first
  consumer to exercise "effective wire ≠ source turn" through the canonical
  `render_selected` path without touching the source envelope.
- New trace field `delta_dropped_part_keys` / evidence `delta_dropped_count`
  let dashboards distinguish an intentional drop from an anomalous payload
  shrink.
