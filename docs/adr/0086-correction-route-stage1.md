# ADR 0086 — Correction route (Stage 1): `correction_triage.kind` drives the follow-up route

- Status: Accepted
- Date: 2026-06-11
- Relates to: ADR 0085 (correction profile and `correction_triage` entry
  phase), ADR 0081 (verification contract scheduling — the `on_phase_pre`
  seam this reuses), ADR 0070 (auto-correction follow-up loop), ADR 0025
  (release gate / `final_acceptance`), ADR 0022 (well-formed REJECTED
  verdicts let the run complete; only hard contract failures halt)
- Extends: ADR 0085. (ADRs are append-only; this records the Stage 1
  routing behavior that ADR 0085 listed under "Out of scope (next
  stages)".)

## Context

ADR 0085 (Stage 0) added the internal `correction` profile and the
read-only `correction_triage` entry phase. Triage classifies the recorded
release blockers into a `kind ∈ {code_fix, contract_ack, gate_rerun,
blocked}` and records a durable verdict, but Stage 0 **always continued
into `implement`** regardless of `kind` — the verdict was observed, not
acted on.

That leaves real ROI on the table and one correctness gap:

- `gate_rerun` / `contract_ack` do not need a code change. The blockers
  are stale (a gate that should be re-run) or a contract/documentation
  acknowledgement. Running `implement` + the `review_changes ↔
  repair_changes` loop spends an implementer and a reviewer round on a
  no-op edit before the closing `final_acceptance` gate.
- `blocked` means triage found no safe remediation path. Continuing into
  `implement` invites the agent to invent a change that triage already
  judged unsafe — tokens spent moving away from a known-blocked state.
- `code_fix` is the one kind that genuinely wants the full chain.

## Decision

Make `correction_triage.kind` the control input for the correction
follow-up route. The route is a pure function of the persisted triage
record and is applied by the orchestrator at the pre-phase seam; the
runner consumes a one-shot skip signal.

### `kind` → route table

| `kind`         | Route |
|----------------|-------|
| `code_fix`     | Continue unchanged: `implement → review/repair → final_acceptance` (Stage 0 behavior, byte-identical). |
| `gate_rerun`   | Skip `implement`, `review_changes`, `repair_changes`; run `final_acceptance` over the retained worktree. No halt. |
| `contract_ack` | Same shortcut skip set as `gate_rerun`; run `final_acceptance`. No halt. |
| `blocked`      | Halt in triage **before any code phase**, `halt_reason="correction_triage_blocked"` (amber operator banner). Unknown/unparseable kinds normalize to `blocked` defensively. |

The shortcut skip set is exactly
`{implement, review_changes, repair_changes}`; `final_acceptance` always
runs for a shortcut route so the closing gate still judges the retained
diff. `derive_correction_route` (in
`pipeline/project/correction_route.py`) is the single pure mapping from
record → `CorrectionRoute(kind, skip_phases, halt, reason)`; a profile
with no triage record yields `None` (routing inactive — the hot path of
every non-correction run is untouched).

### Mechanism: consume-once pre-phase skip channel

Routing rides the existing `on_phase_pre` seam (ADR 0081) — the single
point that can pre-empt a phase **before** its handler / FSM runs — plus a
minimal one-shot channel in the runner:

1. The orchestrator's `_on_phase_pre(name, state)` derives the route. When
   `name ∈ route.skip_phases`, it sets
   `state.extras["_phase_pre_skip_reason"] = route.reason`
   (`PHASE_PRE_SKIP_KEY`). For `code_fix` / non-correction profiles this is
   a strict no-op.
2. The runner, immediately after calling `on_phase_pre`, executes a strict
   order at **both** seam sites (top-level `PhaseStep` and loop-inner
   `_run_loop_step`):

   **pop → halt/handoff → skip**

   - **pop** — `skip_reason = state.extras.pop(PHASE_PRE_SKIP_KEY, None)`,
     unconditional and immediate, so the key can never outlive the phase
     that produced it (no stale key skips an unrelated later phase).
   - **halt/handoff** — if `state.halt` or a pending
     `phase_handoff_request`, break out of the walk. **Halt outranks
     skip**: the popped reason is discarded.
   - **skip** — otherwise, if `skip_reason` is a non-empty string, mark the
     phase skipped via the shared `_skip_phase` helper and `continue`. The
     handler, FSM, quality gates, session adapter, checkpoint, and metrics
     stages are all bypassed — parity with the resume-skip path. The skip
     fires `on_phase_start` / `on_phase_end` only, so banner/trace channels
     stay coherent.

3. **Verification gates do not run for a skipped phase** — neither side
   of it. On the pre side, the orchestrator's `_on_phase_pre` returns
   right after marking the skip, so `before_phase` / `before_delivery`
   gates are not evaluated for a phase that will not execute. On the end
   side, `_skip_phase` exposes a skip-end context for exactly the
   duration of its `on_phase_end` call —
   `state.extras["_phase_end_skipped"] = <phase>` (`PHASE_END_SKIPPED_KEY`),
   removed in a `finally` — and the orchestrator's `_on_phase_end`
   suppresses `after_phase` gate evaluation when it sees that context.
   Without this, an active verification contract with an
   `after_phase(implement)` gate would run gate commands (and could
   repair/halt/handoff) against an `implement` that never executed. The
   context is an extras key rather than a `phase_log["skipped"]` check
   because loop phases legitimately leave handler-side skip records in
   earlier rounds and still execute later ones — a stale marker must not
   suppress real gates.

This is **correction-specific routing**, not a general branching DSL and
not a change to the profile schema. The channel carries one opaque reason
string; the runner does not know about correction kinds. No new profile
fields, no new wire shapes.

### Why not re-resolve the profile after triage?

The rejected alternative was to inspect `kind` after triage and dispatch a
second, trimmed profile (e.g. one that starts at `final_acceptance`). A
double dispatch breaks the run's identity assumptions: phase-handoff
resume and checkpoint replay key off a single contiguous profile walk and
its `completed_phases` set. Re-entering `run_profile` with a different
profile mid-run would desynchronize the checkpoint's notion of which
phases completed, and a handoff pause raised inside the second walk could
not be resumed against the first. Keeping one profile walk and skipping
phases in place preserves resume/checkpoint coherence; the FSM's
`_persist_halted_step` already guarantees a halted (or skipped) phase is
never written as a completed checkpoint step, so no extra cleanup is
needed.

### Evidence contract

- **Route block.** On the non-halted path, triage phase-end stamps the
  flat route dict (`{kind, skip_phases, halt, reason}`) onto both
  `state.phase_log["correction_triage"]["route"]` and
  `session["phases"]["correction_triage"]["route"]` — the operator's
  visible "why these phases were skipped" record in `session.json`. (The
  session adapter already promoted the triage record inside the FSM, so the
  mirror is stamped by hand at phase-end.)
- **Skip reasons.** Each skipped phase records
  `phase_log[phase]["skipped"] = route.reason`. `emit_phase_log_end`
  renders the grey `↳ skipped: <reason>` line and emits
  `outcome="skipped: <reason>"` in the `phase.end` event; the DONE summary
  renders the phase with a `skip` chip (not `ok` and not `fail`). A
  `blocked` run renders `correction_triage=halt`.

### Test directives (mock)

Two deterministic `--mock` directives drive route coverage without a real
model:

- **`orcho-mock-triage-kind: <kind>`** — written into
  `correction_context.md`. The triage prompt embeds the recorded context
  verbatim, so the mock reads the directive and emits a triage record of
  the named kind (`blocked` carries a concrete blocker). Absent or
  unsupported directive → default `code_fix`, so existing Stage 0 mock
  expectations are unchanged.
- **`ORCHO_MOCK_RELEASE_REJECT`** — env-gated (truthy `1/true/yes/on`),
  following the `ORCHO_MOCK_IMPLEMENT_INCOMPLETE` precedent. When armed,
  the mock release gate returns a schema-valid `REJECTED` payload so the
  rejected-acceptance path is exercisable. Unset/falsy leaves the default
  `APPROVED` release behavior untouched.

## Schema / MCP impact

Additive and CLI/session-only:

- The new `halt_reason="correction_triage_blocked"` and the
  `session["phases"]["correction_triage"]["route"]` block are **additive** —
  no existing field changed, no profile JSON changed, and the
  `_phase_pre_skip_reason` channel lives in `state.extras` (never
  serialized to the profile/wire shape).
- The profile schema, mode flags, and gate primitives are unchanged, so
  the **MCP Validation** rule (wire-format/profile/mode/gate changes must
  ship matching `orcho-mcp` updates + an E2E mock smoke) does **not**
  fire. No `orcho-mcp` synchronization is required for this stage.

## Consequences

- A `gate_rerun` / `contract_ack` correction follow-up now costs a scoped
  triage read + the closing `final_acceptance` gate — no wasted
  implementer/reviewer rounds.
- A `blocked` verdict stops the run honestly at triage with an operator
  banner instead of burning tokens past a known-blocked state.
- `code_fix` is byte-identical to Stage 0; non-correction profiles never
  touch the routing path (`derive_correction_route` returns `None`).
- The pre-phase skip channel is a reusable, narrowly-scoped seam, but its
  only consumer is correction routing. A skipped shortcut round still spins
  the review/repair loop once (until-falsy, `max_rounds=1`) and increments
  the round metric, but the round records `skipped`, not work.

## Out of scope (next stages)

- **Generic routing FSM / branching DSL.** This stage is a fixed
  four-kind table consumed by correction only. A declarative,
  profile-authored routing primitive is explicitly not introduced here.
- **MCP / UI representation of the route.** Surfacing the route block (or
  the skip reasons) on the MCP evidence/run surfaces or any UI is deferred;
  this stage is CLI + `session.json` only.
- **Worktree-state-aware shortcuts.** `final_acceptance` keeps its existing
  no-diff halt: a shortcut route over a *clean* retained worktree honestly
  halts on "no diff" rather than approving nothing. Realistic correction
  follow-ups reuse a dirty retained worktree, so this is acceptable; a
  diff-aware shortcut is not part of this stage.
