# ADR 0115 — Finalization Reducer: one settle decision, meta.json as a strict state machine

- **Status:** Accepted (pillar finalization-reducer COMPLETE — see Slice 4)
- **Date:** 2026-06-27
- **Deciders:** project owner
- **Pillar:** 3 of the control-state realignment (pillar 1 = ADR 0112 ParticipantSet; pillar 2 = ADR 0114 RunControlState)
- **Relates to:** [ADR 0114](0114-run-control-state-projection-and-classifier-ownership.md)
  (the read model this validates against), [ADR 0109](0109-supersede-stale-final-acceptance-rejection.md)
  and [ADR 0089](0089-delivery-receipt-continuity.md) (point-fixes this
  generalizes), [ADR 0090](0090-require-gate-no-silent-green.md) (the
  forced-REJECTED backstop the reducer must preserve)
- **Supersedes:** nothing (append-only)

## Context

`meta.json` is a durable fact but **not a strict state machine**: phases write
markers (`halt_reason`, `commit_delivery`, `rejected_outcome`,
`superseded_by_followup`, `no_op_outcome`, …) and many readers/writers interpret
and re-patch them locally. When one writer clears one marker but leaves a related
one, different surfaces see different realities — exactly the stale-correction /
phantom-rejection class of bug observed this cycle.

The 2026-06-27 audit found the same verdict/outcome computed in many places, and
three distinct categories of drift:

**Release verdict re-judged 5× in `finalization.py` alone** — `_release_outcome_token`
(`:354`), `_attempt_approved` (`:268`), `_final_acceptance_rejected_signal`
(`:1755`), `_final_acceptance_rejected_without_diff` (`:1609`) — plus
`commit_delivery.py:318` and `sdk/run_control/delivery.py:_is_rejected_release_gate`
(`:100`). The `final_acceptance` handler emits the verdict once
(`final_acceptance.py:106-147`); everyone downstream re-reads
`verdict`/`ship_ready`/`approved`.

**Terminal-patch matrix duplicated** — `_resolve_terminal_status` (`:1552`),
`_apply_no_diff_final_acceptance_outcome` (`:1672`),
`_apply_rejected_release_terminal_outcome` (`:1843`), the `run.py:_run_commit_delivery`
status→halt_reason ladder (`:1280-1349`), `commit_delivery.apply_commit_delivery`
tokens (`:603-780`), `gate_repair` abort (`:576`), and `sdk/run_control/delivery.py:_settle_run`
(`:793-848`) each translate {delivery status, verdict, no-diff shape} → (status,
halt_reason).

**Three divergent stale-key eviction lists** — `_supersede_stale_rejection_residue`
(`finalization.py:1818`, 5 keys), `_supersede_parent_correction_after_followup`
(`:2003`, 7 keys), and `sdk _settle_run` (`:818`, pops only `halt_reason`). And
markers that are **written but never cleared**: `no_op_outcome` (`:1689`),
`correction_fixed_point` / `correction_not_converging` (`correction_followup.py:410-411`).
Cleared `phase_handoff` leaves sibling extras (`override`/`waiver`/`human_feedback`/
`state.last_critique`) stale.

These are not separate bugs; they are one missing abstraction: **no single place
reduces phase facts into the run's settled state.**

## Decision (proposed)

**Invariant:** phases emit **facts** (their verdict/evidence, never a terminal
patch). Exactly **one finalization reducer** consumes {final-acceptance/CFA
verdict, delivery-status token, `state.halt`, no-diff shape} and decides, in one
place: (a) phase outcome, (b) release outcome, (c) the terminal patch (status +
halt_reason), (d) the **single canonical set of stale keys to clear on settle**,
(e) the delivery decision routing. `pipeline/run_state/terminal.py` `mark_run_*`
remain the reducer's mechanical **output primitives**, not decision sites.

The reducer absorbs (the audit's absorption list):

- **Phase outcome:** `_done_phase_outcome` (`:114`), `_attempt_approved` (`:268`),
  the event reducer `_phase_end` (`run_state/reducer.py:128`), and the duplicated
  incomplete-predicate pair (`review_changes.py:61` ≡ `final_acceptance.py:86`).
- **Release outcome:** the 5 re-reads above → one verdict read.
- **Terminal patch:** `_resolve_terminal_status`, `_apply_no_diff_*`,
  `_apply_rejected_release_terminal_outcome`, the `run.py` matrix,
  `apply_commit_delivery` tokens, `gate_repair` abort, `sdk _settle_run` → one
  mapping (sharing the single halt_reason map from ADR 0114 §2).
- **Stale-key clear:** the three divergent lists → **one** eviction set; and the
  never-cleared markers (`no_op_outcome`, `correction_fixed_point`,
  `correction_not_converging`) join it or are removed.
- **Delivery decision:** keep `resolve_commit_delivery` / `apply_commit_delivery`
  as the engine; route SDK `decide_delivery` + `delivery_decision_state`
  **through** the reducer instead of re-deriving allowed actions / halt_reasons /
  done-settling.

**Preserved invariants:** the ADR 0090 required-receipt forced-REJECTED backstop
and the ADR 0108 provenance overlay keep full authority (the reducer routes them,
never softens them). Ordering invariants stay: diff-capture before delivery;
`run.end` reads post-delivery status (`finalization.py:28-39, 2038-2047`).

## Prerequisite: control-loop test harness

This pillar has the **highest blast radius** in the codebase — the riskiest sites
(`_apply_rejected_release_terminal_outcome` + `_supersede_*`, `:1843-2019`) mutate
`meta.json`, flip `done ↔ halted`, and even rewrite a **parent run's** meta; a
reducer bug there can silently green a rejected release or corrupt a sibling run.
Synthetic-meta unit tests are precisely why this debt hid (the same root as the
R1 theater smoke).

Therefore a **control-loop integration harness is a prerequisite**, not a
follow-up: a test that drives `core writes durable state → SDK projection → MCP
typed tool → resume/delivery action` end-to-end (mock pipeline, real subprocess,
real worktree), so the reducer is validated against ADR 0114's read model on the
real loop, not on hand-set meta.

## Scope / non-goals

- Depends on **ADR 0114** landing first (a single read model to validate the
  reducer's output against) and on the control-loop harness.
- **Not** the read model itself (0114). This is the write/settle side.
- Single-project first, but the **cross-project mirror was traced (2026-06-27):
  folding it does NOT double the work** — it is "substrate + cross leaves" (ADR
  0047), mostly already shared. Per-alias child outcomes come from the mono reducer
  running inside each child (`run_project_pipeline`); delivery transport reuses mono
  `resolve/apply_commit_delivery` (ADR 0049 verified); the terminal-write +
  stale-key clear already route through the shared
  `run_state/terminal.py:settle_cross_terminal`, which clears exactly **one** key in
  **one** place — cross *reinforces* the single-eviction-list goal, it does not add
  a divergent list. Genuinely additive cross scope = two N-fold aggregators (CFA
  precondition→`ParsedRelease` fold in `cross_project/final_acceptance.py:252-368`;
  `cross_delivery._aggregate:401-415`) plus a cross halt_reason **leaf set** on the
  shared status skeleton (`cross_final_acceptance_*`, `cross_delivery_*`, in
  `cross_project/finalization.py:_decide_status:269`). Two NEW cross residue hazards
  the reducer must fold into the single eviction set: `cross_ckpt["pending_gate"]`
  has no clear site after the gate resolves (cross analogue of the stale rejected
  `commit_delivery` gate), and `phase_handoff_kind` is partial-cleared (planning_loop
  clears pending+id but leaves `kind`; cfa_gate clears `kind` but leaves
  alias/child_id). Cross terminal halt taxonomy (`_decide_status`) and the
  kind-discriminated handoff topology stay cross-specific.

## Sequencing

Within this pillar, after 0114 + harness:
1. **Extract the reducer** behind the harness with the **single stale-key list**
   first (this alone kills the residue-bug class) — lowest-risk, highest-bug-value.
2. **Migrate the terminal-patch sites** onto the reducer one at a time, the
   riskiest (`_apply_rejected_release_*`, `_supersede_*`, the parent-meta rewrite)
   **last**, each green on the harness before the next.
3. **Route delivery decision** (`decide_delivery` / `delivery_decision_state`)
   through the reducer.
4. Then **trace + fold cross** as a separate scoped step.

## Guard tests

- One eviction set: a settle-to-`done` from any path leaves **no** residue marker
  (`rejected_outcome`, `no_op_outcome`, `halt_reason`, `correction_*`,
  `delivery_override`, stale `commit_delivery`) — asserted for every terminal
  transition, not just the supersede paths.
- A rejected release cannot finalize as clean `done` (ADR 0090/0108 preserved).
- The control-loop harness reproduces the original stale-correction bug and proves
  it cannot recur (parent superseded only as a consequence of child completion,
  one verdict source).

## Consequences

- `meta.json` becomes a state machine with one transition function; surfaces stop
  seeing contradictory realities.
- The residue-bug class (stale rejection, never-cleared markers, divergent
  eviction) is closed structurally, not patched per-incident.
- Risk is concentrated and contained: the highest-blast-radius writes move behind
  one tested reducer instead of being scattered across seven sites.

## Related

- [ADR 0114](0114-run-control-state-projection-and-classifier-ownership.md) — the
  read model this validates against (land first).
- [ADR 0109](0109-supersede-stale-final-acceptance-rejection.md),
  [ADR 0089](0089-delivery-receipt-continuity.md) — point-fixes this generalizes.
- [ADR 0090](0090-require-gate-no-silent-green.md),
  [ADR 0108](0108-verification-provenance-gate-consistency.md) — backstops the
  reducer must preserve, never soften.
- [ADR 0112](0112-multi-project-participant-set-and-scope-expansion-resetup.md) —
  pillar 1; ParticipantSet feeds the multi-repo delivery the reducer settles.

## Delivery log (append-only)

### Slice 1 — single canonical stale-key eviction set (delivered)

The first sequencing step ("Extract the reducer … with the **single stale-key
list** first") landed as a focused extraction, ahead of the full reducer:

- **One source of truth** in `pipeline/run_state/terminal.py`:
  `TRANSIENT_SETTLE_KEYS` (the named set) + `evict_transient_settle_keys(state)`
  (the one helper, working over any `MutableMapping`, so the same canon serves
  the in-memory `session`, a `meta.json` body, and a superseded parent's meta).
  Each key carries an in-code per-key justification (point of write → why it is
  transient at a *settled* terminal and not read by a legitimate later phase).
- **Three divergent sites collapsed onto the canon**, hand-rolled key-lists
  removed: `_supersede_stale_rejection_residue` (`finalization.py`),
  `_supersede_parent_correction_after_followup` (`finalization.py`, target
  `parent_meta`), and the SDK delivery settle done-branch
  (`sdk/run_control/delivery.py`).
- **Previously-never-cleared markers folded in:** `no_op_outcome` and
  `correction_fixed_point` are now in the canonical set (both proven read only
  by halted display, never by a resume predicate or later phase).
  `correction_not_converging` is **not** a separate key — it is a *value* of
  `halt_reason`, already evicted by clearing `halt_reason`.
- **`phase_handoff` siblings — resolved by fact, not assumption:**
  `phase_handoff_override`, `human_feedback`, and `state.last_critique` are
  **not** keys of the flat settle mapping at all (they live on `state.extras` /
  the runtime `State` object), so the mapping-level helper neither can nor
  should touch them. `phase_handoff_waiver` **is** a top-level mapping key but is
  a *durable* operator waiver record read by evidence collection **after**
  settle (`pipeline/evidence/collector.py`); evicting it would drop legitimate
  audit state, so it is deliberately retained (not residue). `phase_handoff`
  itself is in the canon (already cleared by `mark_run_*`; listed so the set is
  self-contained).
- **Eviction-only invariant held:** no verdict / settle decision-logic changed.
  The conditional delivery eviction (`commit_delivery` /
  `multi_project_delivery`) stays on each call-site's own guard — the phantom-
  gate / companion-mirror guard in `_supersede_stale_rejection_residue` and the
  unconditional delivered-follow-up clear in
  `_supersede_parent_correction_after_followup` — never folded into the
  unconditional canonical set.

### Remaining follow-up

- **Cross-mirror is out of scope for slice 1** (untouched, as planned):
  `cross_project/handoff.py` (the 4-key cross-mirror eviction),
  `cross_ckpt["pending_gate"]` with no clear site, and the partial-cleared
  `phase_handoff_kind` remain divergent cross residue hazards to fold into the
  single eviction set in the later cross step (§"Scope / non-goals",
  §Sequencing step 4).
- The remaining reducer steps (terminal-patch migration of
  `_apply_rejected_release_terminal_outcome` / `_supersede_*` rewrite, the
  `done ↔ halted` flip, the 5× release re-judge consolidation, and routing
  `decide_delivery` through the reducer) are still pending per §Sequencing.

## Slice 2 — single release-verdict source (delivered)

Collapses the **read-side** release-verdict derivations into one module,
`pipeline/run_state/release_verdict.py` (pure string logic, no imports — safe to
import from both `pipeline/*` and `sdk/*`):

- `is_release_blocked(verdict, *, empty_blocks)` — the single non-`APPROVED`
  guard. `empty_blocks` carries the one legitimate per-consumer difference
  (parity-preserving): `commit_delivery.resolve_commit_delivery`
  (`empty_blocks=True` — a gating profile with no recorded APPROVED is blocked)
  vs `run.py` `rejected_release` and the SDK delivery guards
  (`_is_rejected_release_gate` / `decide_delivery` / `delivery_decision_state`,
  `empty_blocks=False` — nothing to refuse on an empty verdict). This fixed a
  latent parity bug the planner's first attempt introduced (a naive single
  predicate would have stopped treating an empty verdict as blocked in
  `commit_delivery`).
- `is_approved` / `is_rejected` — the strict APPROVED / REJECTED verdict-value
  mappings; the DONE-summary read mappers (`_done_phase_outcome` release branch,
  `_attempt_approved`, the release-outcome mapper) and the recovery-lineage
  `_rejected_release` now read them instead of open-coding `== "APPROVED"` /
  `== "REJECTED"`.

Six `!= "APPROVED"` blocked-predicate sites collapsed to one. A grep-invariant
test (`tests/unit/pipeline/run_state/test_release_verdict.py`) pins that the
literal lives only in the single source (plus the deferred cross site), so a new
open-coded guard cannot silently reappear. Eviction-only invariant of slice 1 is
unaffected; no verdict OUTCOME changed (parity asserted on APPROVED / REJECTED /
empty across both `empty_blocks` modes).

### Remaining follow-up (slice 3 — the reducer write side)

- The cross-aggregation verdict (`pipeline/cross_project/cross_delivery.py`
  `_child_verdict(child) != "APPROVED"`) is the one deliberately-deferred
  `!= "APPROVED"` site (cross is a separate follow-up; slice 2 is mono-only).
- The terminal-outcome WRITE re-judges in the reducer/supersede functions
  (`_final_acceptance_rejected_without_diff`, `_final_acceptance_rejected_signal`,
  `_supersede_*`) still carry their own verdict-value checks — these belong to
  the reducer step (terminal-patch + `done ↔ halted` ownership), not the read-side
  consolidation, and route through the single source there.

## Slice 3a — reducer-side verdict detectors on the single source (delivered)

Finishes the verdict-value consolidation on the **write/reducer side** of
`finalization.py`: the no-diff outcome detectors
(`_final_acceptance_rejected_without_diff` / `_final_acceptance_approved_without_diff`),
the rejected-signal detector (`_final_acceptance_rejected_signal`), and the
supersede-residue phantom-gate guard (`_supersede_stale_rejection_residue`) now
read `release_verdict.is_rejected` / `is_approved` / `is_release_blocked`
(`empty_blocks=False`) instead of open-coding `verdict.upper() == "APPROVED" /
"REJECTED"`. `finalization.py` now carries **zero** open-coded release-verdict
literals; a focused test pins that. Detector-only — no terminal-outcome write
logic changed; full pytest green.

## Slice 3b-1 — terminal-outcome reducer seam, two low-risk sites (delivered)

The first structural step of slice 3b introduces the single terminal-outcome
reducer in its focused home `pipeline/run_state/terminal_outcome.py` (NOT a new
block in `finalization.py`) and routes the two **low-risk** terminal sites
through it without changing any terminal outcome or the `finalize_project_run`
ordering:

- `_resolve_terminal_status` (pre-delivery, called @2054) now extracts run facts
  and calls `terminal_outcome.resolve_terminal_outcome`; its open-coded body —
  including the open-coded `session['status'] = 'awaiting_human_review'` write —
  is deleted.
- `_apply_no_diff_final_acceptance_outcome` (post-delivery, called @2075) now
  delegates to `terminal_outcome.apply_no_diff_terminal`; the no-diff verdict
  detectors (`_final_acceptance_rejected_without_diff` /
  `_final_acceptance_approved_without_diff` / `_has_no_diff_final_acceptance_target`)
  move into the reducer module so it is the single home of the no-diff decision.

**F1 closed:** `terminal.py` gains `mark_run_awaiting_review` next to
`mark_run_done` / `mark_run_halted`. The reducer writes **all three** terminal
statuses (`done` / `halted` / `awaiting_human_review`) **only** through
`terminal.py` primitives — zero open-coded `status =` for any branch and zero
re-introduced `== "APPROVED"` (verdicts read only through the
`release_verdict.py`-derived detectors). The new primitive repeats the prior
open-coded write byte-for-byte: it sets only `status='awaiting_human_review'`,
does **not** clear `phase_handoff`, and writes **no** `halt_reason` — the
plan/research pause keeps the active handoff for the operator decision still
ahead. The reducer's own display markers (the nested `halt` compat block,
`no_op_outcome`, `no_change_outcome`) keep their exact dict shapes; only
`status` / `halt_reason` travel through `terminal.py`.

`TRANSIENT_SETTLE_KEYS` / `evict_transient_settle_keys` (slice 1) and
`release_verdict.py` (slice 2/3a) are untouched; `terminal.py` changed only by
adding the new primitive.

### Remaining (slice 3b-2 / 3b-3 — the high-blast WRITE path) — CLOSED

The high-blast WRITE path has since landed in two focused increments behind the
control-loop harness, completing slice 3b for the mono path:

- **Slice 3b-2** (prior commits) migrated the core terminal WRITE re-judges into
  the reducer: `_apply_rejected_release_terminal_outcome` now delegates the
  `done ↔ halted` flip to `terminal_outcome.resolve_rejected_release_terminal`,
  and the `_supersede_*` parent-meta rewrites delegate to
  `terminal_outcome.supersede_same_run_residue` / `supersede_parent_meta`. The
  reducer owns the flip and the display markers; the finalization seam keeps the
  fact extraction + file IO.
- **Slice 3b-3** (below) folds the remaining open WRITE site — the SDK
  `decide_delivery` settle — onto the same reducer.

See the `Slice 3b-3` section below for the SDK-settle closure and the one piece
still carried forward (the **cross-fold**, an explicitly separate step).

### Slice 3b-3 — SDK delivery settle через reducer (delivered)

The last open terminal WRITE site on the **mono** path — the SDK commit-delivery
settle in `sdk/run_control/delivery.py::_finalize` — now routes its `done ↔
halted` flip through the single reducer, closing slice 3b for the mono path.

- **New reducer entry point.** `terminal_outcome.settle_delivery_terminal(meta,
  *, applied_status, halt_reason, halted_at=None) -> str` owns the SDK settle
  flip and the canonical eviction. It carries its OWN delivery-done input set
  `_DELIVERY_DONE_STATUSES = {committed, applied_uncommitted, skipped}` —
  deliberately distinct from the rejected-reconcile `_DELIVERY_APPLIED_STATUSES`
  (which omits `skipped`, because a skip ships no diff). The done branch is
  `mark_run_done` + `evict_transient_settle_keys` (the just-overwritten
  `commit_delivery` is intentionally left intact — the canonical set never
  touches delivery keys); the halted branch is `mark_run_halted(halt_reason,
  halted_at)` with NO eviction. It returns the terminal outcome string so the
  caller carries it onto the result without re-reading `status`.
- **Open settle removed from `_finalize`.** The lazy
  `from pipeline.run_state.terminal import (evict_transient_settle_keys,
  mark_run_done, mark_run_halted)` inside `_finalize` is deleted and the open
  `mark_run_done` / `evict` / `mark_run_halted` branch bodies are replaced by the
  single `settle_delivery_terminal(...)` call. `terminal_outcome` is taken from
  the reducer's return value, not recomputed by a parallel path. The reducer is
  imported at module top (import-safe — `delivery.py` already imports
  `pipeline.run_state.release_verdict`; no cycle). Delivery-executor knowledge
  stays in the SDK: `halt_reason` (`COMMIT_DELIVERY_HALT_REASONS`), `halted_at`
  (`datetime.now(UTC).isoformat`), `accepted`, `blocker`, and the
  `DeliveryDecisionResult` assembly are unchanged result-shaping — the reducer
  receives a ready-made `halt_reason` / `halted_at` and computes no timestamp.
- **Wire-form unchanged (no `orcho-mcp` / E2E needed).** The public
  `DeliveryDecisionResult` / `delivery_decision_state` shape (fields, allowed /
  blocked actions, `terminal_outcome` values, refusal statuses) is byte-stable:
  the existing `tests/unit/sdk/test_delivery_decision.py` assertions pass with no
  expectation edits. `decide_delivery` / `delivery_decision_state` keep the
  single release-verdict read through `is_release_blocked(..., empty_blocks=False)`
  with **no** parallel halt_reason / done-settling logic; the shipping-refusal
  branches stay pure result-shaping (no meta writes) and do not duplicate the
  settle.
- **Cross-surface parity pinned on a real run.** A control-loop harness test
  drives a real mock run to a parked APPROVED gate and to a REJECTED
  final-acceptance terminal and proves the SDK settle and the finalization
  terminal agree through the one reducer: APPROVED → `done`, REJECTED stays
  `halted` / `final_acceptance_rejected` with shipping actions refused
  (`terminal_outcome='halted'`). The drift invariant fails if either surface ever
  flips a verdict to the wrong terminal. The eviction parity
  (`test_eviction_sdk_delivery_settle_path`) continues to pass through the same
  reducer.

**Slice 3b (mono) is complete.** The single terminal-outcome reducer
(`pipeline/run_state/terminal_outcome.py`) is now the only home of the terminal
status flip and the display markers for every mono finalization site: the
pre-delivery status, the no-diff reconcile, the rejected-release reconcile, the
parent supersede, and the SDK delivery settle.

**Carried forward — the cross-fold (separate step).** The cross-project residue
fold is explicitly NOT part of slice 3b mono and remains a distinct increment:
`cross_ckpt["pending_gate"]` (the divergent cross stale-key with no single
eviction site), the partial-cleared `phase_handoff_kind`, and a
`settle_cross_terminal`-style cross terminal taxonomy must still fold onto the
single reducer + canonical eviction set. That is its own focused follow-up, not
rushed into the mono closure.

## Slice 4 — cross residue fold + cross-verdict single source (delivered)

The carried-forward cross-fold lands, closing the last divergence the mono
reducer left open. It is "substrate + cross leaves" exactly as the
2026-06-27 trace predicted (§"Scope / non-goals"): cross *reinforces* the
single-eviction / single-verdict goals rather than adding parallel ones, and
the genuinely cross-specific aggregators + halt taxonomy stay cross-owned.

- **Settle-only vs handoff-only eviction split + single-settle `pending_gate`.**
  The one prior cross stale-key with *no* clear site (`cross_ckpt["pending_gate"]`)
  and the *partial-cleared* `phase_handoff_kind` are resolved by two DISJOINT
  canonical sets in `pipeline/run_state/terminal.py`:
  `CROSS_SETTLE_RESIDUE_KEYS = ("pending_gate",)` + `evict_cross_settle_residue`
  (the **settle-only** set) and `CROSS_HANDOFF_MARKER_KEYS`
  (`phase_handoff_id`/`_kind`/`_project_alias`/`_child_id`, **no** `pending_gate`)
  + `evict_cross_handoff_markers` (the **handoff-only** set, which also flips
  `phase_handoff_pending=False`). Disjointness is load-bearing: `pending_gate`
  goes stale at *settle* (the run is over), the kind markers go stale at
  *handoff consumption* (the operator decision is applied). `settle_cross_terminal`
  gained an optional `cross_ckpt` and is now the **single settle-only clearing
  point** for `pending_gate` — it evicts the session mirror always and the
  persisted checkpoint copy when threaded. Every cross terminal funnels through
  it: the normal done/failed tail (`finalize_cross_run` threads `ctx.cross_ckpt`)
  and the early-return terminals (`finalize_cross_terminal` — ABORT→cancelled,
  the halt paths). The three `planning_loop` partial-clear paths and the two
  duplicated `handoff.py` blocks now route through `evict_cross_handoff_markers`;
  the `cfa_gate` shared-marker clear routes through it too while keeping
  `cfa_paused_state` popped locally with its own justification. No cross site
  carries a hand-rolled key-list anymore.
- **Single verdict source via `release_verdict`.** The one deliberately-deferred
  `!= "APPROVED"` literal (slice 2's "Remaining follow-up") — the per-child
  not-approved decision in `cross_delivery.py` — now routes through
  `release_verdict.is_approved` (parity with the mono delivery guards). The
  grep-invariant test (`test_release_verdict.py::test_blocked_predicate_is_single_source`)
  dropped `cross_delivery.py` from its whitelist, so `release_verdict.py` is now
  the **only** site carrying the non-approved literal across `pipeline/` + `sdk/`.
- **Preserved cross-specific aggregators / taxonomy (unchanged).** The two
  N-fold cross aggregators stay cross-owned: the CFA precondition→`ParsedRelease`
  fold (`cross_final_acceptance.py`) and `cross_delivery._aggregate` (the
  per-alias success/failure/halt rollup). `_decide_status`'s cross halt leaf set
  on the shared status skeleton (`cross_final_acceptance_*`, `cross_delivery_*`,
  `phase_handoff_halt`) and the kind-discriminated handoff topology remain
  cross-specific — the reducer routes them, never softens them. The ADR 0090
  forced-REJECTED backstop and ADR 0108 provenance overlay keep full authority.
- **Harness (verification choice).** The slice-4 spec prefers a full multi-alias
  mock cross run in `tests/integration/control_loop/`, but a real cross run only
  reaches a settled terminal by dispatching one child `run_project_pipeline` per
  alias through real git-worktree isolation (the heaviest test class) plus
  deterministic CFA/delivery steering — disproportionate for the narrow residue/
  verdict invariants this slice changed. Per the done-criteria escape hatch the
  parity is instead pinned through the cross slice with a REAL settle (not
  synthetic meta): `tests/unit/pipeline/cross_project/test_cross_settle_residue_parity.py`
  drives the production settle writers (`finalize_cross_run` /
  `finalize_cross_terminal` / `evict_cross_handoff_markers`), persists
  `meta.json` + `cross_checkpoint.json` to a real run dir, and reloads BOTH from
  disk to assert (1) a settled terminal carries no `pending_gate` (incl. the
  gate-resume restore path) and no stale `phase_handoff_kind`/siblings, with the
  two eviction sets proven disjoint, and (2) the settled cross verdict agrees
  with `release_verdict` (`is_approved` ⇒ `done` without delivery halt_reason;
  otherwise a cross halt-leaf). The per-child cross-delivery routing through
  `is_approved` is additionally covered by the existing `git_worktree`
  `test_cross_delivery.py` override-gate tests.

**Pillar finalization-reducer (ADR 0115) is COMPLETE.** `meta.json` is now a
state machine with one settle decision on both the mono and cross paths: one
terminal-outcome reducer (`terminal_outcome.py`), one canonical mono eviction
set (`TRANSIENT_SETTLE_KEYS`), one disjoint cross settle/handoff eviction pair,
and one release-verdict source (`release_verdict.py`). This builds on the
cross substrate of [ADR 0047](0047-cross-project-application-boundary.md)
and preserves the [ADR 0090](0090-require-gate-no-silent-green.md) /
[ADR 0108](0108-verification-provenance-gate-consistency.md) backstops the
reducer routes but never softens.
