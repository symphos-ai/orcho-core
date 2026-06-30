# ADR 0090 — Require-gates cannot end in a silent green run

- Status: Accepted
- Date: 2026-06-13
- Relates to: ADR 0081 (scheduled gate routing, Stage 4), ADR 0082
  (final-acceptance readiness awareness, Stage 5), ADR 0083 (delivery-gate
  awareness, Stage 6), ADR 0080 (native command-receipts, Stage 3), ADR 0089
  (delivery receipt continuity), ADR 0025 (release gate shape), ADR 0022
  (soft-fail halt scope)

## Context — the silent-skip incident

Run `20260612_224612_3527d4` (workspace `atas/workspace-orchestrator`, project
`bot_1`) completed `status=done` with `final_acceptance=ok` although the
project's verification contract scheduled four required dotnet gates
(`policy=require` at `after_phase(implement)` and `before_delivery`). The
evidence bundle showed `verification_readiness={commands: [], envs: []}`,
`contract_status.tests="missing"`, an *empty* `verification_gaps`, and the
final-acceptance reviewer's own REJECTED verdict (P1 "Missing required
verification receipts", `ship_ready=false`) — yet the run delivered the
unverified diff back to the project and reported green.

Forensics found four independent defects that compounded:

1. **Wrong verification subject.** `pipeline.project.state_setup` built the
   verification `PlaceholderContext` with `checkout = project_path` — the
   *original* project directory — although its own docstring promised the
   worktree checkout. Every gate command therefore ran against the pristine,
   already-built original repo: `dotnet build` / `dotnet test` passed in
   seconds without ever seeing the implement diff, so the Stage 4 routing
   concluded "gates passed" and never repaired, paused, or warned.
   (`{project}` and `{checkout}` were the same string, so the distinction the
   Stage 3 executor documents — provenance from the subject, resources from
   the project — was vacuous.)
2. **Receipts never persisted.** `write_command_receipt` (Stage 3, ADR 0080)
   had no production caller: the Stage 4 router executed commands and dropped
   the receipt payloads. Stage 5 readiness and the Stage 6 delivery gate read
   receipts from disk, so they always classified every required command
   **missing** — even when the router had just run it.
3. **Delivery policy stuck at warn.** `resolve_delivery_policy` (ADR 0083)
   reached `require` only via an explicit contract `delivery_policy` field.
   A contract that scheduled `policy=require` gates *at the delivery
   boundary* still delivered with policy `warn`, so "missing required
   receipts" was a banner, not a block.
4. **REJECTED release verdict not load-bearing.** Per ADR 0022/0025 a
   well-formed REJECTED `final_acceptance` records the critique and lets the
   run complete; pause semantics live in handoff policies, and the `advanced`
   profile declares none for `final_acceptance`. With 1–3 broken there was no
   remaining layer that could stop the run.

A contributing environment fact (not an orcho-core defect, but recorded by
the ADR 0076 receipts): the run's `pipeline_import` env-check resolved
`pipeline` to a *foreign* dev worktree because the MCP supervisor spawns the
runner with the full inherited environment of the MCP server process —
including a leaked `PYTHONPATH` from an unrelated orcho-on-orcho development
session. The receipt detected this (its purpose) but nothing consumes receipt
failures, and for non-Python projects the check is structurally red
(`expected=None`). Sanitizing the spawn environment belongs to `orcho-mcp`;
this ADR only fixes what core owns.

## Decision

Layered, deterministic enforcement — a `require` gate that did not produce a
passing receipt can never end in a green, delivered run without an explicit
operator waiver:

1. **The verification subject is the run worktree.**
   `_verification_placeholder_context` now receives the run's `git_cwd` and
   sets `checkout` to it (fallback: project path when isolation is off).
   `{project}` keeps pointing at the original project for stable resources
   (e.g. gitignored SDK dirs). Gate commands, env assertions, and
   touched-path selection all operate on the worktree.
2. **The Stage 4 router persists receipts.** `gate_repair._run_gate_command`
   writes every executed gate receipt via `write_command_receipt` under
   `<run_dir>/verification_command_receipts/` (latest execution wins).
   Readiness, the delivery gate, and evidence now see the same proof the
   routing decision was based on.
3. **Scheduling `require` at the delivery boundary IS the delivery opt-in.**
   `resolve_delivery_policy` derives `require` when any schedule entry at
   `before_delivery` carries an explicit `policy: "require"`. An explicit
   `delivery_policy` field still wins, and `work_mode` still never escalates.
   This supersedes the ADR 0083 nuance that `require` was reachable only via
   the explicit field.
4. **Engine backstop at the closing gate.** After parsing the release
   verdict, `final_acceptance` merges engine-computed `verification_gaps`
   (one per required delivery command whose receipt classifies
   missing/failed/stale, built by
   `verification_readiness.required_receipt_gaps`) and forces
   `approved=False / verdict=REJECTED / ship_ready=False`, recording an
   `engine_backstop` block in the phase log. The backstop is inert under
   dry-run, without a contract, or when an operator waiver
   (`continue_with_waiver`) is active — the waiver is the explicit human
   decision the contract demands. ADR 0022's scope is preserved: the handler
   still does not *halt*; blocking is owned by the gate handoff and the
   delivery gate.

The operator escape hatch is unchanged and explicit: the gate handoff offers
`continue_with_waiver`, and a recorded waiver disarms the backstop while
remaining durable in meta/evidence.

## Consequences

- A broken verification environment (gate command cannot run) pauses the run
  at `verification_gate_failed` instead of completing done; locked by the
  acceptance suite (`tests/acceptance/test_verification_gate_blocking.py`),
  including the receipt-cwd-is-worktree assertion.
- Runs whose gates pass now leave passing receipts on disk, so the Stage 5
  readiness block stops reporting them missing (the prior state made the
  final-acceptance reviewer distrust genuinely verified runs).
- Contracts that schedule `require` at `before_delivery` now block delivery
  on missing/failed/stale receipts (`commit_delivery_verification_blocked`)
  rather than warn.
- Reviewer-model omissions can no longer hide unproven required gates:
  `verification_gaps` always carries the engine-computed entries, which flow
  into evidence and the release report's open risks.
- No wire-format change: receipt schema (v2), release schema, profile shape,
  and gate primitives are unchanged; `orcho-mcp` needs no matching update.

## Out of scope

- Sanitizing the spawned runner environment (`PYTHONPATH` leak) — belongs to
  `orcho-mcp`'s supervisor.
- Making the ADR 0076 `pipeline_import` env-check meaningful for non-Python
  projects (today it is structurally red there and consumed by nobody).
- A handoff policy for `final_acceptance` steps outside loops — the gate
  handoff plus the delivery gate already provide the pause; widening the
  step-level handoff support matrix stays a separate decision.
