# ADR 0092 — CI handoff advice (Stage 1): `ci_agent` policy-controlled auto-retry

- Status: Accepted
- Date: 2026-06-13
- Relates to: ADR 0031 (generic phase-handoff contract — the four canonical
  actions, `available_actions`, the strict decision-artifact reader, and the
  `decide ≠ resume` split this builds on), ADR 0090 (handoff advice Stage 0 —
  the interactive `advice` / `retry_with_advice` pseudo-actions, the durable
  advice artifact, and the `note`-based provenance this stage reuses verbatim),
  ADR 0070 (auto-correction follow-up loop — the unattended counterpart this
  stays distinct from), ADR 0087 (allowed modifications — the plan-level write
  allowance the scope gate draws on)

## Context

ADR 0090 (Stage 0) closed the rejected/incomplete handoff loop **for an
operator at a TTY**: a read-only advisor recommends the smallest honest way
forward and, when the operator accepts, the generated feedback flows through the
existing `retry_feedback` decide + resume path. Stage 0 was deliberately
interactive-only — its "Non-goals" section names *unattended / CI auto-retry* as
a future stage, precisely because letting an advisor decide on nobody's behalf
needs explicit budget and safety controls before it can be trusted.

Stage 1 supplies those controls. In a **non-interactive (CI / `--no-interactive`
/ piped)** run the handoff loop previously had only one move: persist the pause
and exit `rc=4` for an operator to resolve off-band. We want an eligible
rejected/incomplete handoff in that mode to be able to **auto-retry once**
through the advisor — under a bounded budget and audited safety gates — and
otherwise stop with a typed reason, so CI can make forward progress on the
honest, low-risk cases without ever silently applying a risky change.

The hard constraints are the same audit-grade ones Stage 0 faced. The strict
decision-artifact reader (`_read_existing_strict`) rejects unknown fields by
design; any wire-format change to the decision artifact, the profile shape, the
mode flags, or the gate primitives would pull in a matching `orcho-mcp` update
and an E2E mock smoke under the **MCP Validation** rule. Stage 1 must therefore
add no new decision field and no new profile/mode surface.

## Decision

Add a **policy-controlled, prompt-free CI sub-flow** that runs in
`process_pending_phase_handoffs` exactly where the interactive prompt would run,
when `should_prompt_for_phase_handoff()` is `False`. It reuses the Stage 0
advisor primitives (context build, read-only invoke, parse, safety classifier),
the Stage 0 durable advice artifact, and — crucially — the **same**
`sdk.phase_handoff.phase_handoff_decide` + `apply_phase_handoff_resume_with_banners`
decide/resume path the human `retry_feedback` uses. There is **no** parallel
repair branch: the CI sub-flow only produces the `feedback` + `note` inputs the
existing path already consumes, or a typed stop.

### `ci_agent` as a third feedback source

A `retry_feedback` decision now records one of **three** provenance sources in
its free-text `note`, using the Stage 0 shape:

```
feedback_source=<human | agent_advice | ci_agent>; advice_artifact=<relative path>
```

- **`human`** — the operator typed the feedback (no advice artifact; the bare
  decision the canonical menu always produced).
- **`agent_advice`** — Stage 0: an operator accepted (or edited) an advisor
  recommendation at the TTY.
- **`ci_agent`** — Stage 1 (this ADR): the non-interactive sub-flow auto-applied
  an advisor recommendation under policy.

`ci_agent` is carried through the **unchanged** `note` field, built from the
exact path the advice-artifact write returned. The decision artifact format, the
four canonical actions, `available_actions`, and the SDK decision validation are
**unchanged**. The human `retry_feedback` path and the Stage 0 interactive
`retry_with_advice` path are untouched.

### Internal policy object (no profile/mode change)

Mode resolution and the budget/gate parameters live in an **internal**
immutable dataclass `HandoffAdvicePolicy`, resolved from `run.no_interactive`:

- `auto_retry_with_agent: bool` — `False` for interactive/TTY runs (they keep
  the Stage 0 operator-driven behaviour and never auto-retry), `True` for
  non-interactive runs.
- `max_agent_retries: int = 1` — an **explicit, typed budget** with a safe
  default of one auto-retry. It is a field on the object (overridable in tests),
  **not** a profile knob or a mode flag, so adding it triggers no MCP Validation.
- `require_human_for: tuple[str, ...]` — the auditable list of reasons that
  always stop for a human rather than auto-retry: `waiver`, `scope_change`,
  `destructive_action`, `repeated_p1`, `advice_confidence_low`.

Moving this policy into the **profile** (so a project could declare its own CI
auto-retry budget/gates) is a deliberate future step that **would** change the
profile wire shape and therefore require an `orcho-mcp` update + E2E mock smoke;
it is out of scope here.

### Safety gates (`evaluate_ci_gates`)

A recommendation auto-applies **only** when it is a `retry_feedback` at non-`low`
confidence, budget remains, it is not a waiver, it is not a repeated blocking
finding, no destructive marker is recognised, and its expected files stay within
scope. The gates are evaluated in a fixed order and each non-proceed outcome is a
typed `stop(reason, state)` with `state ∈ {needs_operator, halt, budget_exhausted,
repeated_finding}`:

1. **waiver** — a `continue_with_waiver` recommendation → `stop(waiver,
   needs_operator)`. CI never auto-waives (mirrors the Stage 0 "no automatic
   waiver, ever" rule).
2. **halt** — a `halt` recommendation → `stop(halt, halt)`. The handoff loop
   then sets `state.halt` and clears the pending request, falling through to the
   caller's `run.finalize()` — the **same** handler-side-halt tail a gate abort
   uses — which renders the HALTED summary (no parallel halt path, no decision
   artifact).
3. **other non-retry** — `continue` (or any non-`retry_feedback`) →
   `stop(<action>, needs_operator)`.
4. **low confidence** — a `low`-confidence retry → `stop(advice_confidence_low,
   needs_operator)`. CI cannot supply the operator confirmation Stage 0 requires
   for low confidence, so it stops instead.
5. **budget** — `budget_remaining <= 0` → `stop(budget_exhausted,
   budget_exhausted)`.
6. **repeated finding** — an identical recurring blocking (P1/P2) finding →
   `stop(repeated_finding, repeated_finding)`.
7. **destructive** — a recognised destructive marker → `stop(destructive_action,
   needs_operator)`.
8. **scope** — expected files outside the plan scope →
   `stop(out_of_scope, needs_operator)`; an empty (unlimited) scope proceeds with
   a `scope_unchecked` note rather than blocking.

#### Scope gate

`build_scope(state)` is the union of `state.parsed_plan.owned_files +
allowed_modifications` taken at **both** the plan level and across **every**
subtask. A retry's `advice.expected_files` must each match at least one scope
glob via `fnmatch`; any file outside the union stops with `out_of_scope`. When
`parsed_plan` is `None` or declares no scope, `build_scope` returns the **empty
frozenset as an unlimited marker** — the gate then **proceeds** and records
`scope_unchecked` rather than producing a false stop. This draws the scope from
the same plan-level `allowed_modifications` allowance ADR 0087 defined.

#### Destructive gate

`is_destructive_recommendation(advice)` is an **auditable, positive-recognition**
classifier. It analyses **only** the free-text fields `advice.retry_feedback`,
`advice.risks` (joined), and `advice.operator_note` — `recommended_action`
carries no destructive signal because the enum cannot express one. Matching is a
case-insensitive substring search against a fixed, reviewable module constant
`_DESTRUCTIVE_MARKERS`:

```
rm -rf, git reset --hard, git checkout -- , git restore, git clean,
git push --force, push -f, force push, drop table, truncate table,
delete from, history rewrite, rebase --, reflog expire, wipe, destroy
```

A single marker hit ⇒ destructive ⇒ stop. The **safe default is
non-destructive** (proceed): ambiguous or empty text never trips the gate — it
fires only on positive recognition, mirroring the `_has_blocking_severity`
"P≥3 is provably safe" discipline. The marker list is a module constant so it
stays auditable and extensible without touching the gate logic.

### Bounded budget + counter lifecycle

The CI sub-flow runs inside the existing `process_pending_phase_handoffs` loop;
no new `while`. The bound is `budget_remaining = max_agent_retries - retries`,
and a repeated-finding fingerprint (`(id, severity, title)` over the findings) is
carried across loop iterations so an identical recurring P1/P2 stops instead of
looping forever.

An aggregate at `run.state.extras['_ci_agent_advice']` tracks the lifecycle
(durable per-advice detail stays in the advice artifacts):

```json
{
  "retries": 0, "resolved": 0, "stopped": 0,
  "last_recommendation": "", "last_confidence": "",
  "last_findings_fingerprint": "", "scope_unchecked": false
}
```

- `last_*` are refreshed on **every** CI advisory call.
- A **proceed** increments `retries`, then flows the `ci_agent` decision through
  the shared decide + resume path. If the resumed retry round produced **no**
  fresh rejected/incomplete handoff, `resolved` is incremented; otherwise the
  loop re-evaluates the new pause under the now-smaller budget.
- Every **stop** increments `stopped`. A `halt` stop routes through the caller's
  HALTED finalization (above); all other stops (`needs_operator`,
  `budget_exhausted`, `repeated_finding`, plus the `out_of_scope` /
  `destructive_action` needs_operator stops) return the run **paused** for an
  operator. A stop never records a retry decision.

**Durable flush.** Because a paused stop returns **before** any DONE/HALTED
finalization and `apply_phase_handoff_pause` already saved the session *before*
the aggregate updated, the aggregate is mirrored onto `run.session` (meta.json)
and re-saved at each stop / resolved boundary — so the persisted paused-report
carries the real `stopped` / `last_recommendation` / `scope_unchecked` values,
not only the in-memory view.

The aggregate is seeded **lazily**, only when the auto-retry path actually runs,
so interactive runs and a disabled policy never create it and never invoke the
advisor provider.

### Final summary surfacing

The DONE/HALTED terminal summary renders a compact block from the real aggregate
(when `retries > 0`):

```
Agent advice:
  ci_agent retries=N resolved=N stopped=N
  last recommendation=<...> confidence=<...>
```

A paused `needs_operator` stop does **not** flow through DONE/HALTED
finalization, so its counters are inspected via the persisted
`run.state.extras['_ci_agent_advice']` aggregate; only the resolved (DONE) and
`halt` (HALTED) outcomes reach the summary block.

## Schema / MCP impact

Additive and CLI/session-only — the same deciding factor as Stage 0:

- No new decision field: `ci_agent` provenance rides the existing `note`. The
  decision artifact format, the four canonical actions, `available_actions`, and
  SDK decision validation are **unchanged**.
- The advice artifact (`phase_handoff_advice/`) and its schema are reused from
  Stage 0 unchanged. The `_ci_agent_advice` aggregate is run state, mirrored
  additively onto the run meta (`meta.json`) — not the audit-grade decision
  artifact, so the strict decision reader and its wire contract are untouched.
- `HandoffAdvicePolicy` is an **internal** object; no profile shape, mode flag,
  or gate primitive changed.
- Because no wire-format, profile shape, mode flag, or gate primitive changed,
  the **MCP Validation** rule does **not** fire for this stage. Surfacing the
  policy in the profile, or the `ci_agent` aggregate/advice on the MCP run
  surface, **would** change the wire contract and require an `orcho-mcp` update
  plus an E2E mock smoke in the same change — that is the explicit condition for
  the next stage.

## Consequences

- A CI run can auto-resolve the honest, low-risk rejected/incomplete handoffs in
  one bounded retry through the exact human decide + resume path, instead of
  always parking at `rc=4`.
- Every unsafe recommendation — waiver, halt, continue, low confidence,
  out-of-scope, destructive, repeated blocking finding, or exhausted budget —
  stops with a typed reason and records **no** retry decision, so CI never
  silently applies a risky change.
- Each `ci_agent` retry is auditable end-to-end: the decision's `note` points at
  the exact advice artifact (`feedback_source=ci_agent`), and the run summary
  reflects the real retries/resolved/stopped counters.
- The CI auto-retry **primitive** (policy + gates + bounded lifecycle reusing the
  canonical decide/resume) is reusable: a future *change-pool CI* could drive
  unattended retries through the same primitive without a new handoff mechanism,
  and the advice artifacts + `ci_agent` provenance are sufficient for
  retrospection.

## Non-goals (future stages)

- **Profile-declared CI policy.** Moving `max_agent_retries` / the gate
  configuration into the profile is deferred — it changes the profile wire shape
  and requires an `orcho-mcp` update + E2E mock smoke.
- **MCP / Web surface for `ci_agent`.** Surfacing the aggregate or advice on the
  MCP evidence/run surface or any non-TTY transport is deferred; this stage is
  CLI/session-only.
- **Autonomous waiver / multi-retry escalation.** CI never auto-waives, and the
  default budget is a single retry; a richer escalation policy is out of scope.
