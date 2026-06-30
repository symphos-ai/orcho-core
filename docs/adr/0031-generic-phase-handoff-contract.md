# ADR 0031 — Generic Phase Handoff Contract

- **Status:** Accepted
- **Date:** 2026-05-21
- **Deciders:** project owner
- **Supersedes (public surface only):** parts of [ADR 0022](0022-phase-taxonomy-cleanup.md)
  that documented the validate_plan-specific gate (`orcho_plan_gate_decide`,
  `block_on_plan_reject`, `awaiting_plan_approval`, `validate_plan.gate_blocked`).
  ADR 0022's phase-rename decisions (`plan_qa` → `validate_plan`,
  `build` → `implement`, etc.) stay in force.
- **Companion to:** [ADR 0019](0019-stepoutcome-fsm-active.md) (lifecycle FSM),
  [ADR 0024](0024-cross-profile-projection.md) (cross-project projection)
- **Extended by:** [ADR 0035](0035-terminal-status-and-resume-observability.md)
  (2026-05-24) — the `halt` action also finalizes `evidence.json` +
  `metrics.json` on disk now (this ADR's "halt = writes decision
  artifact, flips meta.status, clears phase_handoff" remains accurate;
  the bundle write is additive). Pause-time also snapshots
  `metrics.json` so the halt-side collector has full rollups to read.
  Cross-subprocess metrics aggregation on `retry_feedback` resume is
  documented there as well.

## Context

ADR 0022 shipped a validate_plan-specific gate machinery as the only
in-pipeline pause point. Over time three problems compounded:

1. **Two parallel sources of truth.** Pause state lived in
   `meta.plan_gate`, `state.validate_plan_gate_blocked`, the
   `AWAITING_PLAN_APPROVAL` status string, a `plan_gate_decision.json`
   artifact, and a `validate_plan.gate_blocked` event — none of which
   referenced each other authoritatively. UI consumers (Web pending
   banner, MCP supervisor, evidence collector) each had their own
   read path.
2. **Global round overrides as the wrong abstraction.** `--max-rounds`,
   `--max-plan-rounds`, and `--block-on-plan-reject` were process-wide
   knobs that affected unrelated loops and made it ambiguous which
   retry budget a caller was tuning. The plan loop's budget is a
   per-loop property of the active profile, not a runtime flag.
3. **Validate_plan-shaped jargon leaking into the contract.** The
   gate name, the status string, the SDK function, the MCP tool, and
   the Web review screen all hard-coded validate_plan even though
   the underlying machinery generalises trivially to any phase that
   wants to pause for human direction.

Slice 1 of the phase-handoff redesign retires the legacy surface and
ships a **declarative, generic, profile-driven pause contract** that
single-project validate_plan-in-plan-loop runs through end-to-end.
Cross-project execution and non-loop generic phases are explicitly
out of scope for slice 1 (see *Out of scope* below).

## Decision

Pause semantics are a declarative profile property, not a runtime
knob. A `PhaseStep` may carry a `PhaseHandoffPolicy`:

```python
PhaseStep(
    phase="validate_plan",
    handoff=PhaseHandoffPolicy(
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
    ),
)
```

Three policy types exist, all human-only in slice 1:

| Type | Trigger semantics |
|------|-------------------|
| `human_bypass` (default; omit equivalent) | Never pauses. |
| `human_feedback_on_reject` | Pauses **only** when (a) verdict is `rejected`, (b) the outer `LoopStep.until` predicate is unsatisfied, and (c) the current automatic round is the **last** by `LoopStep.max_rounds`. |
| `human_feedback_always` | Pauses after every round. `approved` → actions = `["continue", "halt"]`; `rejected` → actions = `["continue", "retry_feedback", "halt"]`. |

The runtime emits a `phase.handoff_requested` event and the
orchestrator persists the active payload to `meta.phase_handoff` with
`meta.status = "awaiting_phase_handoff"`. The subprocess exits with
**rc=4**. From there, three actions resolve the pause:

- `continue` — manual override. The machine verdict (`approved` /
  `rejected`) is **not** rewritten. Resume injects an explicit
  `state.extras["phase_handoff_override"]` marker that lets the loop
  runner exit without mutating `validate_plan.approved` (which would
  re-enter the loop forever).
- `retry_feedback` — runs exactly **one** extra human-directed
  `plan → validate_plan` round. `LoopStep.max_rounds` is not mutated;
  a separate `human_directed_rounds` counter tracks the budget. If
  the extra round is approved the run continues post-loop; if
  rejected a fresh handoff fires at round+1 with a new `handoff_id`.
- `halt` — terminal. Writes the decision artifact, then synchronously
  flips `meta.status` to `halted` and clears `meta.phase_handoff`.

### Built-in profile matrix

| Profile | `validate_plan.handoff` |
|---------|-------------------------|
| `lite` | `human_bypass` (explicit) |
| `advanced` | `human_feedback_on_reject` |
| `enterprise` | `human_feedback_on_reject` |
| `plan` | `human_feedback_always` |
| `review` | n/a (no validate_plan step) |
| `task` | n/a (no validate_plan step) |

`plan` profile **does not** end via `awaiting_human_review` anymore.
Both `approved` and `rejected` verdicts pause via
`awaiting_phase_handoff` with the matching action set.

## Load-bearing invariants

### 1. `decide ≠ resume`

The two operations are intentionally separate verbs:

| Verb | What it does | What it does NOT do |
|------|--------------|---------------------|
| `phase_handoff_decide(run_id, handoff_id, action, feedback?, note?)` | Writes the decision artifact. `halt` synchronously flips `meta.status`. | Never spawns a process. `continue` / `retry_feedback` leave `meta.status="awaiting_phase_handoff"` until resume fires. |
| `orcho_run_resume(run_id)` | Spawns a fresh subprocess, reads `meta.phase_handoff` + the matching decision artifact, applies action semantics, continues execution. | Does not record decisions. Refuses to resume a run whose latest decision is `halt`. |

This split exists so that MCP / Web / CLI clients can:

- Record a decision **without** spawning a subprocess (e.g. record now,
  resume later, or resume from a different transport).
- Show a "decided but not resumed" UI sub-state (the decision artifact
  is on disk but `meta.status` is still paused).
- Audit the exact human instruction that drove the resume, separately
  from the resume execution log.

`orcho_run_status` exposes this sub-state via `phase_handoff_decided`
(bool) + `phase_handoff_decision` (the persisted record) so UI
consumers switch the form to a "Resume" affordance once a decision
exists.

### 2. Exact-payload idempotency

`phase_handoff_decide` is idempotent **only on a full match** of the
persisted record. For the same `handoff_id`:

| Replay shape | Behaviour |
|--------------|-----------|
| Same `action` + same `feedback` + same `note` | Success replay. Artifact is **not** rewritten, `decided_at` is **not** refreshed. Returns the persisted record unchanged. |
| Same `action`, different `feedback` / `note` | `InvalidPhaseHandoffState` (conflict). |
| Different `action` | `InvalidPhaseHandoffState` (conflict). |

This makes MCP retries and UI double-submits safe without letting them
silently mutate the audit text. The strict reader also defends against
corrupted artifacts (mismatched persisted ids → conflict) and against
`safe_handoff_id` hash collisions (the hash is computed off the raw
`handoff_id` string).

**Halt-after-halt special case.** A `halt` artifact remains the audit
source of truth even after `meta.phase_handoff` is cleared. Replaying
the exact same `halt` payload is idempotent against the artifact alone
(no active payload required). A different payload for the same
`handoff_id` is still a conflict.

### 3. `meta.phase_handoff` is the canonical active payload

There is **one** authoritative source for active-handoff state:
`meta.phase_handoff`. Everything else is a derived view:

- `run.session["phase_handoff"]` (if still used by older UI plumbing)
  is a compat mirror generated from `meta`, not a second source of
  truth. Web reads `meta` directly; MCP surfaces `meta.phase_handoff`
  on `orcho_run_status`.
- `phase_handoff_decisions/{safe_handoff_id}.json` is the audit log,
  not the active state — it persists after the handoff is resolved
  and after `meta.phase_handoff` is cleared on `halt`.
- The `phase.handoff_requested` event records the *moment* the
  handoff fired; it does not represent current state.

Halt invariant: on halt, the decision artifact is written **first**,
then `meta.status` flips to `halted`, then `meta.phase_handoff` stops
being treated as active. Pending-queue filters (Web banner, MCP
supervisor) key on `status + active payload`, not on a stale payload.

### 4. Cross-project fail-fast is intentional

Slice 1 runtime support is **single-project validate_plan in a plan
loop only**. Cross-project orchestration does not yet implement
`continue` / `retry_feedback` / `halt` execution semantics.

Built-in `advanced` / `enterprise` / `plan` profiles declare non-bypass
handoff on `validate_plan`. When `orcho cross --profile advanced`
(or enterprise / plan) projects those profiles for cross execution,
`pipeline.cross_project.profile_projection` **fail-fasts** with a
structured error:

> cross-project phase handoff lands in a later slice. Use `human_bypass`
> on validate_plan or switch to the `lite` profile for cross runs.

This is **the expected behaviour**, not a bug. The alternative —
silently dropping the handoff policy at cross-projection time — would
break the declarative contract: a profile that declares
`human_feedback_on_reject` would silently behave as `human_bypass`
when invoked from cross. Fail-fast is the safer default until
cross-runtime support lands.

Re-enabling cross execution under these profiles requires either
(a) a cross-specific projection/bypass strategy, or (b) the full
cross-runtime handoff implementation in a future slice. Both are
out of scope for slice 1.

### 5. Decision artifacts are an audit log

Each handoff lands its decision at
`<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json` where
`safe_handoff_id` is a deterministic, collision-resistant encoding of
the `handoff_id` (sanitised slug + short SHA-256 hash). Multiple
handoffs in the same run coexist as separate files; the directory is
append-only by SDK convention.

Schema:

```json
{
  "run_id":     "20260520_120000_abcdef",
  "handoff_id": "validate_plan:plan_round:2",
  "phase":      "validate_plan",
  "action":     "retry_feedback",
  "feedback":   "Add the auth-migration step before deployment.",
  "note":       "operator: missed auth migration; one more round",
  "decided_at": "2026-05-20T12:01:30+00:00"
}
```

Three transports (CLI/SDK, MCP, Web) all write through the same
SDK function (`sdk.phase_handoff.phase_handoff_decide`) so the audit
shape is identical regardless of who initiated the decision. CLI's
TTY resolver writes the artifact **before** continuing/halting — no
"phantom" interactive decisions without an audit record.

## Slice 1 runtime support matrix

| Executor | Non-bypass handoff support |
|----------|----------------------------|
| Single-project profile runner | `validate_plan` inside a plan loop only |
| Single-project non-loop phase | Unsupported → fail-fast before execution |
| Cross-project projection / orchestrator | Unsupported → fail-fast at projection time |
| MCP / Web decision API | Only decides an active persisted `phase_handoff` payload |

The profile loader **accepts** `handoff` on any `PhaseStep` (shape
validation only). Phase-name and execution-context checks are runtime
concerns — the loader does not predict what the runtime can execute.

## Consequences

### Public surface changes (intentional breaking change across CLI / SDK / MCP / Web)

Per the project's no-backcompat-ceremony rule (single-developer
project, no installed base for internal plumbing), no aliases or
shims were shipped. Update direct callers.

**Retired:**

- CLI flags: `--max-plan-rounds`, `--block-on-plan-reject`, `--max-rounds`
  (the last as a defaults source — `--max-rounds` still exists as the
  review/repair loop runtime cap)
- Config keys: `pipeline.max_plan_rounds`, `pipeline.block_on_plan_reject`
- Env vars: `MAX_PLAN_ROUNDS`, `BLOCK_ON_PLAN_REJECT`
- `run_pipeline` / `run_cross_pipeline` kwargs: `max_plan_rounds`,
  `block_on_plan_reject`
- SDK: `sdk.validate_plan_gate.py` deleted; `ValidatePlanDecision`,
  `validate_plan_decide`, `load_validate_plan_decision`,
  `InvalidValidatePlanGateState` removed from `sdk.__init__`
- `PipelineStatus.AWAITING_PLAN_APPROVAL` deleted
- `EventKind.VALIDATE_PLAN_GATE_BLOCKED` + its payload schema deleted
- MCP: `orcho_plan_gate_decide` tool deleted;
  `orcho_run_start` no longer accepts `max_plan_rounds` /
  `block_on_plan_reject`
- Web: launch-form "Plan rounds" and "Block Build if Plan QA didn't
  approve" controls removed; `plan_gate_review` state renamed
  `phase_handoff_review`
- orcho-ui-kit: `filter_pending_gates` now keys on
  `awaiting_phase_handoff`; `build_nav` kwarg renamed
  `pending_plan_gates` → `pending_phase_handoffs`

**Introduced:**

- `PhaseHandoffType` / `PhaseHandoffAction` enums in `pipeline.runtime.roles`
- `PhaseHandoffPolicy` dataclass; `PhaseStep.handoff: PhaseHandoffPolicy | None`
- `PipelineStatus.AWAITING_PHASE_HANDOFF`
- `EventKind.PHASE_HANDOFF_REQUESTED`
- `sdk.phase_handoff` module: `phase_handoff_decide`,
  `load_phase_handoff_decision`, `load_phase_handoff_decisions`,
  `load_active_phase_handoff`, `safe_handoff_id`, `PhaseHandoffDecision`,
  `InvalidPhaseHandoffState`
- MCP tool: `orcho_phase_handoff_decide(run_id, handoff_id, action,
  feedback?, note?)`; `orcho_run_status` exposes `meta.phase_handoff`
  + `phase_handoff_decided` + `phase_handoff_decision`
- Web `phase_handoff_review` state with three SDK-backed actions
  (continue / retry_feedback / halt), pause budget read from profile,
  decided-but-not-resumed sub-state surfaced

### Out of scope (future slices)

- Cross-project `continue` / `halt` / `retry_feedback` runtime support
- Generic non-loop phase handoff execution
- Expert per-loop budget overlay UI/contract (replacement for retired
  global round knobs)
- `HumanReview` / `AWAITING_HUMAN_REVIEW` deletion (definitions
  remain reserved/no-op; cleanup is a separate slice after cutover)

### Migration

See [`docs/migration/validate-plan-gate-retired.md`](../migration/validate-plan-gate-retired.md)
for the legacy → new mapping table, CLI / SDK / MCP / Web before/after
snippets, and the cross-project fail-fast note for callers who relied
on `awaiting_plan_approval` semantics.

## Notes

ADR 0022's changelog entries describe the legacy contract those
phases shipped — they're kept as historical record. The phase-rename decisions from 0022 (`plan_qa` →
`validate_plan` etc.) are still in force; only the gate-specific
public surface bits supersede.
