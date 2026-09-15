# Session Shape

> Phase 3 reference. <!-- TODO(orcho-phase-7): expand with full
> field-by-field schema for each phase once orcho.session_adapters
> entry_points group is wired in Phase 7. -->

The pipeline's run output is captured in two complementary stores:

1. **`session.json`** — the durable, human-readable, dashboard-consumable
   summary. Persisted to `<workspace>/runs/<run_id>/session.json`.
2. **`state.phase_log[name]`** — in-memory per-phase scratch space the
   handler populates. Not committed; orchestrator promotes it into
   `session.json` shape via adapters.

`SessionAdapter` is the contract that translates `state.phase_log[name]`
+ select state fields → `session["phases"][name]`. Adapters live in
`pipeline/session_adapters.py` and are registered in
`SessionAdapterRegistry` (Phase 7 ships `orcho.session_adapters`
entry_points for plugin extension).

## Which "session" this is

Three distinct mechanisms share the word; this doc covers only the first:

1. **`session.json`** — the durable run-summary store (this document).
2. **The provider agent session** — whether a phase invocation *continues*
   the prior provider session or starts fresh. That is profile policy
   (`session_continuity`: `fresh_only` / `loop_continue` /
   `same_zone_continue`), projected by
   `pipeline/runtime/session_disposition.py`
   ([ADR 0113](../adr/0113-session-disposition-policy-and-context-baggage-guard.md));
   see [Prompt Engine](prompt_engine.md) for where that decision sits.
3. **Prompt-session reuse** — how prompt parts are grouped for delta
   rendering inside a continued session (`PromptSessionSplit`:
   `STATELESS` / `PER_PHASE` / `PER_ROLE` / `COMMON`,
   `pipeline/prompts/session.py`).

Adapters here run regardless of those two: whatever the disposition and
delta decisions were, the phase's durable summary lands in `session.json`
the same way.

## Why the split

`SessionAdapter` decouples session-shape ceremony from phase-handler
logic: handlers stay naive (just stuff data into
`state.phase_log[name]`), adapters know the canonical shape and translate
state to `session["phases"][name]`. Customer plugins can override the
session shape per phase via the `orcho.session_adapters` entry-points
group without forking phase handlers.

## Scope guardrail

`SessionAdapter` is for **session-dict shape normalization** —
**not** a universal external-runner integration bus. If we ever need to
import forge-session / codex-session / claude-session transcript shapes
for resume / audit, that becomes a separate concept
(`ExternalRuntimeAdapter` or `TranscriptImporter`). `SessionAdapter`
must not parse foreign transcript JSON, mutate external files, or open
network sockets — adapters are deterministic shape translators only.

## Built-in adapters

| Adapter | Phase name | Session key | Cardinality |
|---------|------------|-------------|-------------|
| `PlanAdapter` | `plan` | `session["phases"]["plan"]` | list (per-round attempts) |
| `ValidatePlanAdapter` | `validate_plan` | `session["phases"]["validate_plan"]` | list (per-round attempts) |
| `BuildAdapter` | `implement` | `session["phases"]["implement"]` | dict (single) |
| `RoundAdapter` | `rounds` (+ `repair_changes` v2-dispatch alias) | `session["phases"]["rounds"]` | list (per review_changes↔repair_changes round) |
| `ReviewRoundAdapter` | `review_changes` | sub-record **inside** the matching `session["phases"]["rounds"]` entry | one per review attempt (`review` / `reverify`) |
| `FinalAcceptanceAdapter` | `final_acceptance` | `session["phases"]["final_acceptance"]` | dict (single) |
| `CorrectionTriageAdapter` | `correction_triage` | `session["phases"]["correction_triage"]` | dict (single; ADR 0085 correction profile triage verdict) |
| `HypothesisAdapter` | `hypothesis` | `session["phases"]["hypothesis"]` | dict (single, optional) |

### `ReviewRoundAdapter` writes into the round, not a phase key

`ReviewRoundAdapter` is the one built-in that writes **no**
`session["phases"][<its phase>]` key. Every `review_changes` dispatch is
persisted as a sub-record inside the round entry it belongs to
([ADR 0193](../adr/0193-final-acceptance-latest-review-context.md)):

```json
{
  "round": 1,
  "critique": "…",
  "repair_receipt": { },
  "review":   { "pass": "review",   "attempt": 1, "verdict": "REJECTED",
                "approved": false, "clean": false, "repair_preceded": false,
                "short_summary": "…", "findings": [ ] },
  "reverify": { "pass": "reverify", "attempt": 1, "verdict": "APPROVED",
                "approved": true,  "clean": true,  "repair_preceded": true,
                "short_summary": "…", "findings": [] }
}
```

- `pass` names the attempt: `review` is the round's first review pass,
  `reverify` the post-repair re-verify pass of
  [ADR 0039](../adr/0039-review-repair-phase-handoff.md). `attempt` is the loop
  round number, so `(round, pass)` is the attempt's identity.
- `repair_preceded` states whether a repair pass had already run in this round
  when the attempt was recorded — the producer reads it off the round entry
  instead of deriving it from the pass. The ordinary in-loop `review` pass
  reviews a pre-repair subject (`false`); a `reverify` is post-repair by
  definition, and so is the operator-feedback retry round, which runs
  `repair_changes -> review_changes` and still stores the round's first
  `review` pass (`true`). Readers use this — not the pass — to say whether a
  round's repair is unverified or already reviewed.
- `parse_error` is added when the attempt's output never parsed. The record is
  written anyway: a reader must be able to see both that the attempt happened
  **and** that its verdict is unusable.
- `session_id` / `continue_session` are copied from the attempt's `meta` when
  present, per-attempt rather than per-round.
- Writing the same `(round, pass)` twice overwrites the same key, so a replayed
  attempt never grows the attempt list.

**Where the pass comes from.** The adapter reads an explicit runner signal that
the current dispatch is the post-repair re-verify pass — never "a `review` key
already exists". Inferring the pass from key presence would relabel a re-run of
the first attempt as a second opinion nobody gave.

**Why not `session["phases"]["review_changes"]`.** The checkpoint callback saves
any phase that *has* a session entry as a completed checkpoint phase, and stamps
a loop cursor for phases in the active loop. `review_changes` is a loop phase, so
creating that key would start writing checkpoint rows and loop cursors for it and
change what loop resume restores. Keeping the record inside `rounds` is purely
additive and invisible to checkpoint/resume.

**Merge order.** The review adapter runs first in the loop and may append a
*provisional* entry (`{"round": n, "review": {…}}`) before any repair happened.
`RoundAdapter` later fills that entry **in place** — matching on `round` and on
the absence of `critique` — carrying the `review` / `reverify` sub-records over
instead of appending a second entry for the same round. A round that already has
a `critique` is a completed round and is never reused.

`FinalAcceptanceAdapter` copies an optional `review_context` key: the
prior-review evidence the closing gate was handed, resolved from these
sub-records. Its shape is documented in
[Run artifacts](../reference/run_artifacts.md#phasesfinal_acceptancereview_context).

## Per-round invocation pattern

The orchestrator stuffs per-round data into `phase_log[name]` and then
invokes the adapter:

```python
self.state.phase_log.setdefault("plan", {}).update({
    "attempt":             round_n,
    "parsed_file_paths":   list(parsed_plan.file_paths),
    "existing_files":      list(existing),
    "missing_files":       list(missing),
    "total_atomic_tasks":  parsed_plan.total_atomic_tasks,
    "codemap_injected":    bool(self.codemap and round_n == 1),
    "hypothesis_injected": bool(self.research_hypothesis and round_n == 1),
    "replan_critique":     (self.state.last_critique if round_n > 1 else None),
    "human_feedback":      (self.state.human_feedback if round_n > 1 else ""),
    "meta": {"human_directed": bool(self.state.human_feedback)},
})
self._session_adapters.get("plan").write(
    "plan", self.state, self.session, round_n=round_n,
)
```

On replan attempts (`round_n > 1`):

- `replan_critique` is the reviewer critique that triggered the retry
  (machine-generated by `validate_plan`). Empty when the retry was
  operator-directed without a prior reviewer rejection.
- `human_feedback` is the operator instruction text supplied via
  `phase_handoff_decide(action="retry_feedback", feedback=...)`. Empty
  when the retry was driven purely by a reviewer rejection.
- `meta.human_directed` is `True` on any attempt that consumed operator
  feedback, so evidence consumers can distinguish reviewer-only retries
  from operator-directed ones without inspecting the body.

Both fields can be non-empty simultaneously: an operator may retry a
plan that the reviewer also rejected, and `tasks/replan.md` teaches the
architect how to reconcile them (operator feedback authoritative,
reviewer critique advisory).

The handler itself only sets `output` + `meta`; the orchestrator (or
Phase 5+ lifecycle FSM) supplies the rest.

## Why adapters now fire from `on_phase_end`

Phase 3 chose explicit invocation because the old `run_*_loop` methods
computed per-round derived data (`parsed_plan.file_paths`,
`_validate_plan_file_paths` results) after the handler ran but before
the adapter fired.

Phase 5d deleted those loop methods. The derived data moved into
handlers / callbacks, and the lifecycle FSM now auto-fires the registered
adapter in its `adapter` stage
(`PhaseLifecycle._fire_adapter` in `pipeline/lifecycle.py` — stage 8,
after the handler and gates, before the checkpoint write);
`_PipelineRun._on_phase_end` keeps only timer/banner duties and the
legacy fallback. Direct adapter calls remain only for special
pre-profile helpers such as hypothesis.

## Plugin override semantics

A plugin's adapter `register("plan", CustomPlanAdapter())` **overwrites**
the built-in. The override is total — orcho doesn't merge or chain.
This matches the legacy "register a custom phase handler" semantics
(`PhaseRegistry.register`) so plugin authors learn one pattern.

## See also

- `pipeline/session_adapters.py` — implementation
- `tests/unit/pipeline/runtime/test_session_adapters.py` — pinned shapes per adapter
- [Phase lifecycle](phase_lifecycle.md) — where adapter firing sits in
  the owner-stage model
