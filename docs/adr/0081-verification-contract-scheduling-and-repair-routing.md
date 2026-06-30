# ADR 0081 — Verification contract scheduling, policy algebra, and repair routing (Stage 4)

- Status: Accepted
- Date: 2026-06-10
- Relates to: ADR 0080 (verification contract native command-receipts, Stage 3),
  ADR 0078 (verification contract env-assertions and CLI, Stage 2),
  ADR 0077 (verification contract read-only projection, Stage 1),
  ADR 0076 (durable verification-environment receipt),
  ADR 0074 (worktree bootstrap)

## Context

Stages 1–3 made the verification contract *loadable, validated, projected, and
on-demand executable* — but still **non-blocking**: `require` gated nothing, the
`work_mode` strictness control derived no policy, and there was no scheduled
transition from a failed gate into repair. Stage 4 closes that gap. It turns the
declared schedule/work_mode/gate_sets/selection into a **deterministic policy
algebra** that decides, per command and per hook, an *effective policy* and an
*effective action*, and routes a failed required gate into the right transition
(repair / handoff / abort) — without re-deriving selection at routing time and
without touching the MCP-facing wire.

The shaping constraints:

- **Absence must differ from an explicit value.** A schedule entry that omits
  `policy`/`action` is *not* the same as one that sets `policy: suggest` or
  `action: abort`. Absence is the input to the work_mode transform; an explicit
  value is an operator's authoritative decision and must not be silently
  transformed or derived away.
- **Determinism.** The same contract + run context must always produce the same
  plan, the same effective policy/action, and the same routing. No ordering or
  set-iteration nondeterminism.
- **Token economy on the critical path.** A failed required
  `after_phase(implement)` gate must reach repair *without* spending a reviewer
  turn — the failing command output is the critique, and re-running the gate
  command is the exit condition.
- **The wire must not move.** Stage 4 is internal orchestration. It must not add
  a field to the evidence v1 bundle, the run header, `meta.json`, or any MCP
  resource. If a Stage 4 primitive were *required* on the wire, that is a
  separate `orcho-mcp` workstream, not a silent core change.

## Decision

### 1. Selection model (T1)

`verification` gains two optional declarations:

- `gate_sets`: `name -> {commands: [...]  (required), default_policy?,
  default_action?, default_cheap?}`. The defaults are **optional** — absence is
  `None`, the input for the work_mode transform, never a silent default.
- `selection`: an ordered list of rules, each declaring **exactly one** type
  key: `{always: [sets]}`, `{task_kind: <str>, include: [sets]}`,
  `{paths: [glob...], include: [sets]}`, `{operator: [sets]}`.

`schedule.policy` and `schedule.action` become **optional** (`str | None`); a
schedule entry may also carry an optional `gate_sets` list that narrows the
defaults-merge source. Commands gain an optional `cheap: bool`.

The contract also declares the **selection intent** — the production source for
the selection context: optional `verification.task_kind` (a task class matched
by `task_kind` rules) and `verification.operator_sets` (the gate sets the
project/profile opts into; operator gate sets activate *only* when named here).
A per-run operator/CLI request may override either via `state.extras`
(`verification_task_kind` / `verification_operator_sets`); absent both, the
inputs are inert (`None` / empty), so default runs select no `task_kind` or
operator gates.

### 2. Policy algebra (T2)

A pure, read-only engine (`pipeline/verification_selection.py`) builds a
deterministic `ScheduledGatePlan`:

- **Selection order** is fixed: `baseline(always)` → `task_kind` →
  `subsystem(paths ∩ touched_paths)` → `operator`, deduped preserving first
  occurrence.
- **Selected commands** are the union of the selected sets' commands, deduped in
  stable order. Each command records `contributing_gate_sets` (every selected
  set containing it) and `primary_gate_set` (the first by the fixed order).
- **Defaults merge** across the contributing sets: `merged_default_policy` =
  max strictness (`off < suggest < warn < require`) among sets that declare one
  (`None` if none); `merged_default_action` = max strictness
  (`continue_warn < repair_loop < handoff < abort`); `cheap` = OR of
  `command.cheap` and any contributing `default_cheap`. A schedule entry's
  `gate_sets` narrows the merge *source* only (not the attribution).
- **Schedule tie-breaker**: multiple schedule entries for one `(command, hook)`
  collapse to one by max strictness; a `None` policy is treated as *absent*
  (an explicit entry participates; all-`None` stays `None`). A command with no
  applicable schedule entry becomes `manual_only`.

**Effective policy** resolution order:

1. explicit `schedule.policy` (not `None`, incl. `suggest`) → authoritative, **not**
   transformed;
2. else `base_policy = merged_default_policy` (may be `None`);
3. else `derive_effective_policy(base_policy, work_mode, required, cheap)` per the
   work_mode table.

**Effective action** resolution order:

1. explicit `schedule.action` (not `None`, incl. `abort`) → authoritative;
2. else `merged_default_action` if set;
3. else the strictly deterministic `derive_effective_action(hook, phase,
   work_mode)`.

The deterministic work_mode-derived action table:

| Hook / phase | `fast` | `pro` | `governed` | unset |
|---|---|---|---|---|
| `after_phase(implement)` | `continue_warn` | `repair_loop` | `repair_loop` | `continue_warn` |
| `before_delivery` | `continue_warn` | `handoff` | **`handoff`** | `continue_warn` |
| anything else | `continue_warn` | `continue_warn` | `continue_warn` | `continue_warn` |

`governed + before_delivery + require` with **no explicit action ⇒ `handoff`**
(never `abort`). `abort` only ever appears through an *explicit* schedule/gate-set
action or a separate terminal/system path — the algebra never derives it.

### 3. Critical-flow repair routing (T4) and the hook matrix (T5)

Routing consumes executable `ScheduledGatePlan`s cached **by lifecycle position**
(`hook:phase`) in `state.extras["verification_gate_routing_plans"]`. Position
keying — not merely hook keying — is required because `after_phase` fires after
*every* phase: `after_phase(plan)` / `after_phase(validate_plan)` run before
`implement`, while `after_phase(implement)` runs after it. Each distinct
`hook:phase` position builds its plan once at that point and reuses it on later
invocations of the same position (deterministic, no per-hook recompute); an early
plan (`after_phase:plan`, `before_phase:implement`, …) is **never** reused for
`after_phase:implement`, whose path-based subsystem selection is built from the
post-implement changed files. This cache is **distinct** from the prompt
projection's advisory preview (`state.extras["verification_gate_prompt_preview"]`),
which routing never reads, so an early prompt build cannot freeze which gates
run. Only `require`-policy gates participate — `off` / `suggest` / `warn` never
block (they are surfaced read-only in the prompt blocks, not executed as gates
here). Per effective action:

- `continue_warn` → log a warning, do not block.
- `abort` → halt the run.
- `handoff` → request a phase handoff (pause).
- `repair_loop` → governed by the **deterministic repair_loop-by-hook matrix**.

The matrix (`repair_loop_target(hook, phase)`):

```text
repair_loop is a real repair flow ONLY for after_phase(implement)
            (and only when the profile has a repair_changes step).
every other hook/phase  ->  deterministically degrades to handoff
                            (with a logged, user-visible note).
```

The `after_phase(implement)` critical flow: synthesize the critique from the
failed command receipt into `state.last_critique` / `state.last_test_output`,
dispatch `repair_changes` through the lifecycle FSM **without** a preceding
`review_changes` pass, then **re-execute the same gate command** as the exit
condition. A passing re-check closes the flow; budget exhaustion
(`--max-rounds` / the profile's `repair_round` loop) escalates to a handoff.

**Integration points.** The hooks fire *inside* `run_profile` via per-phase
callbacks, not after the whole profile has run:

- `before_phase` and `before_delivery` fire through a **pre-phase seam**
  (`run_profile(on_phase_pre=…)`, wired to `run._on_phase_pre`). This runs
  *before* the phase handler — the only point that can pre-empt a phase, since
  the lifecycle FSM checks `state.halt` only *after* the handler executes. A
  `require` gate that aborts/handoffs here stops the phase from running at all.
  `before_delivery` fires when the about-to-run phase is a delivery boundary
  (`FINAL_PHASES`), blocking only on a failed/missing/stale `require` receipt.
- `after_phase` fires through the **phase-end seam** (`run._on_phase_end`),
  immediately after the phase finishes and *before* `run_profile` advances to
  the next entry. For `implement` this means the critical repair flow runs and,
  on a paused/aborted disposition, `run_profile` breaks **before** the
  `review_changes` loop — a reviewer turn is never spent on a known-bad state.
- `on_resume` fires on the checkpoint-resume path before dispatch continues.
- `manual_only` is never auto-planned.

Re-entrancy is guarded: the `after_phase(implement)` repair flow dispatches
`repair_changes`, which re-fires the phase callbacks; an `_in_gate_hook` flag
stops nested gate evaluation. All callbacks are contract-gated pure no-ops when
no contract is declared, so the no-contract dispatch path stays byte-identical.

The pre-phase seam is a new optional `on_phase_pre` callback on
`pipeline.runtime.run_profile` / `_run_loop_step` (default `None` → inert for
every other caller). It is in-process orchestration only — it carries no data to
any durable artifact and does not touch the MCP wire.

### 4. Prompt projection (T6)

When the contract declares `gate_sets`/`selection`, the per-phase prompt block is
projected from the resolved plan (`render_phase_gate_block`) carrying the
**effective** `policy -> action` and the gate source via `primary_gate_set`,
placeholder-resolved and limited to the phase's relevant entries (`plan`,
`implement`, `review_changes`, delivery). A contract with only the Stage 1
schedule still uses the read-only schedule projection (`render_phase_block`).
Blocks stay RUN-scoped dynamic `PromptPart`s; the whole config is never dumped.

The Orcho default prompt policy is code-owned; the
`workspace → project → run` override chain remains a TODO.

## Consequences

- A project can now declare gate sets + selection + work_mode and get
  deterministic, blocking gate behavior with a token-cheap repair path on the
  critical `implement` gate, while every other hook degrades `repair_loop` to a
  visible handoff rather than silently doing nothing or looping forever.
- Absence vs explicit is now a load-bearing distinction operators can rely on:
  `policy: suggest` and `action: abort` survive untransformed; omission flows
  through the work_mode transform.
- The `ScheduledGatePlan` is an in-memory value (memoized in `state.extras`); it
  is **not** serialized to any durable wire artifact.

## MCP wire falsifier

**Claim: Stage 4 does NOT change the MCP-facing wire shape. No `orcho-mcp`
update is required for this change.**

Evidence:

1. **The plan is in-memory only.** `ScheduledGatePlan` / `ScheduledGateEntry`
   live in `pipeline/verification_selection.py` and are cached under
   `state.extras` (the epoch-keyed executable `verification_gate_routing_plans`
   and the advisory `verification_gate_prompt_preview`). They are never written to
   `meta.json`, the evidence v1 bundle, or any receipt. `pipeline/evidence/schema.py`
   (`REQUIRED_TOP_LEVEL_KEYS`, `REQUIRED_COMMAND_KEYS`) has **no** slot for a gate
   plan, gate sets, selection, effective policy, or effective action.
2. **The header projection is name-only and unchanged in shape.**
   `render_header_summary` is the single contract→text projection feeding the
   printed run header. It surfaces `work_mode` / env names / command names /
   schedule policies only — never `gate_sets`, `selection`, `action`,
   `required`, the plan, or receipts. Stage 4 adds no new header segment (a
   `None` schedule policy renders as `derived` inside the existing `schedule=`
   segment — same field, not a new one).
3. **No MCP resource projects the changed surface.** There is no MCP module in
   `orcho-core`; `orcho-mcp` is a separate package outside this checkout. The
   existing MCP read tools project the evidence v1 bundle / run state, both
   unchanged. New Stage 4 symbols (`build_scheduled_gate_plan`,
   `ScheduledGatePlan`, `repair_loop_target`, `run_gate_hook`,
   `render_phase_gate_block`, `gate_sets`, `selection`) are referenced only
   inside `pipeline/` routing/projection and prompt building.

**Stop condition (deferred `orcho-mcp` workstream).** If a later requirement puts
the plan, `work_mode`, or any gate primitive *on the wire* (a new evidence key, a
`meta.json` field, or an MCP resource), this falsifier flips: that becomes a
separate `orcho-mcp` workstream — owned files in the `orcho-mcp` repository, a
dependency on Stage 4's T1/T2 contract shape, a mock E2E, and a named
integration point — not a silent `orcho-core` change. `orcho-mcp` is a separate
repository outside this checkout.

The conclusion is pinned by the non-e2e mock smoke
`tests/acceptance/mock_pipeline/test_smoke_matrix.py` (marker `mcp_integration`,
inside the `not e2e and not packaging` gate): it builds a Stage 4 contract,
resolves a plan, and asserts the header projection and evidence v1 schema carry
no Stage 4 primitive. If a future change leaks a gate primitive onto either
surface, that smoke fails and the stop condition above applies.
