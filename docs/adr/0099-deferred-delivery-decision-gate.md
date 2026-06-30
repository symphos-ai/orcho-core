# ADR 0099 — Deferred delivery decision gate and out-of-band decide surface

Status: Accepted

## Context

The post-release commit-delivery gate (ADR 0032) resolves *inline*: a
non-interactive run reads `commit.auto_in_ci` and either auto-approves the diff
into the project checkout or — for a non-APPROVED release / `not_applicable`
case — silently drops it. There is no way for a headless controller (an MCP
client, a web dashboard, a supervisor agent) to let an operator decide the
delivery of a finished non-interactive run *after the fact*. The inline prompt
(`resolve_commit_delivery`) is the only operator entry point, and it requires a
TTY.

Two capabilities were missing:

1. A run that finishes non-interactively should be able to **park** its
   delivery decision as a recoverable gate, holding the run instead of
   auto-shipping or silently dropping the diff.
2. A sanctioned, **out-of-band** decision entry point a client can call to
   resolve that parked gate — applying the same release / verification / dirty
   policy the inline path enforces, without re-implementing any of it.

## Decision

Two internal slices, one engine, one config flag.

### Engine: injected action + parking switch

`resolve_commit_delivery` gains two keyword-only parameters, both inert on the
default path so historical CLI/CI behavior stays byte-identical:

- `decision_action: CommitDeliveryAction | None` — injects an operator-chosen
  action into a NON-interactive resolve, replacing the `auto_in_ci` default.
  The hard guards stay in force: a non-APPROVED release still refuses
  `approve` / `apply` (they return `not_applicable`), while `fix` / `skip` /
  `halt` remain expressible. Ignored in interactive mode and on the auto path.
- `decision_mode: 'auto' | 'defer'` — provider-neutral parking switch.
  `'defer'` (non-interactive only) returns a `pending` decision (`action='none'`)
  carrying the full persistent context instead of auto-resolving it.

### Producer (slice C2): `commit.decision_mode`

`core.infra.config` adds `commit.decision_mode` (default `'auto'`). In `'defer'`
mode the producer (`pipeline/project/run.py::_run_commit_delivery`) persists the
parked decision to `meta.commit_delivery` (status `pending`) and finalizes the
run `halted` with the recoverable `halt_reason='commit_delivery_pending'`,
WITHOUT touching the project checkout. The amber finalization banner and the
`is_terminal_resume_parent` classifier recognise the new halt reason so
checkpoint-resume never auto-selects a parked run.

### Executor (slice C1): `sdk.decide_delivery` / `delivery_decision_state`

A new run-control surface (`sdk/run_control/delivery.py`, re-exported as
`sdk.decide_delivery` and `sdk.delivery_decision_state`):

- `decide_delivery(run_id, action, note=None, ...) -> DeliveryDecisionResult`
  replays the parked gate through the engine executors. It loads
  `meta.commit_delivery`, re-checks the hard guards from the persisted evidence
  (rejected release, required verification), re-resolves the diff against the
  held worktree with the operator action injected, applies it, and finalizes
  the run. No decidable gate → `accepted=False, blocker='no_pending_delivery_gate'`
  (never an exception).
- `delivery_decision_state(run_id, ...) -> DeliveryDecisionState` is the
  read-only projection feeding a client's gate UI: `decidable`, `kind`
  (`delivery` / `correction` / `none`), `available_actions`, `blocked_actions`,
  `default_action`, `reason`. It is the single authoritative source for which
  actions a client may offer.

`RunService.decide_delivery(DeliveryDecisionCommand)` mirrors the function as a
thin injected-callable delegation (same pattern as `decide_handoff`).

### `patch_text` is never read back

`CommitDeliveryDecision.patch_text` is NOT serialised by `to_dict()`. The
executor therefore reconstructs the delivery context purely from the persisted
keys (`source_path` / `project_path` / `baseline_ref` / `changed_paths` /
`untracked_paths`) by re-resolving the diff against the held worktree — the
patch is recomputed, never read from an in-memory field. This also re-checks
release / verification policy freshly at decision time.

### `terminal_outcome` is strictly the run's terminal status

`DeliveryDecisionResult.terminal_outcome` is `Literal['done', 'halted']` and
nothing else. The 'correction marked' state (an accepted `fix` that did not
start a follow-up) is expressed by the combination
`status='fix_requested'` + `halt_reason='commit_decision_fix'` +
`followup_run_id=None` — never by a 'correction marked' value in
`terminal_outcome`. The SDK never starts a correction follow-up synchronously
(`drive_correction_followups` is TTY-only), so `followup_run_id` is always
`None` from this surface.

## Consequences

- The public packages gain a sanctioned, TTY-free delivery decision surface
  that any embedder can drive; `orcho-mcp` exposes it as `orcho_delivery_decide`
  + `delivery_gate.next_actions` ready-calls.
- Default `decision_mode='auto'` keeps every existing CLI/CI delivery path
  byte-identical; parking is strictly opt-in.
- `meta.commit_delivery` now carries a new `status='pending'` value at rest in
  defer mode. The decision-artifact schema (`commit_decisions/<id>.json`) is
  unchanged — `pending` lives only on the in-meta gate context, never in the
  schema-validated audit artifact.

## References

- [ADR 0032 — commit-decision gate](0032-commit-decision-gate.md)
- [ADR 0069 — delivery dialog on rejected acceptance](0069-delivery-dialog-on-rejected-acceptance.md)
- [ADR 0083 — verification contract delivery-gate awareness](0083-verification-contract-delivery-gate-awareness.md)
- `docs/reference/run_artifacts.md` — `meta.commit_delivery` shape and halt-trigger tables
