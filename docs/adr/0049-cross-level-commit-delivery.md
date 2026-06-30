# ADR 0049 — Cross-level commit delivery

- **Status:** Accepted
- **Date:** 2026-05-28
- **Deciders:** project owner
- **Extends:** [ADR 0032](0032-commit-decision-gate.md),
  [ADR 0043](0043-commit-delivery-apply-action.md),
  [ADR 0047](0047-cross-project-application-boundary.md)

## Context

Single-project runs deliver their run-owned diff into the project
checkout through `pipeline/engine/commit_delivery` (ADR 0032 / 0043).
The mono path early-returns for cross children
(`pipeline/project/run.py` — when `parent_run_id` / `project_alias` is
set), on the assumption that the cross level would orchestrate one
bundled delivery instead of N child prompts.

That cross-level delivery was never built. Confirmed live (run
`20260528_163401`): a cross run whose system release gate
(`cross_final_acceptance`, CFA) returned APPROVED finished `done`, but
neither project checkout received a commit and the run dir held no
`commit_decisions/`. Every per-alias change survived only inside the
retained run worktree (`runspace/runs/<id>/worktrees/wt_<alias>/checkout`)
plus the Phase-0 recovery hint. For a delivery tool, an APPROVED run
that ships nothing is a P0 correctness defect, not a gap.

## Decision

Add `pipeline/cross_project/cross_delivery.py` with
`run_cross_delivery(...)`. The cross runner calls it **before**
`finalize_cross_run` (the single emitter of `run.end`) once the CFA gate
returns `approved_terminal` or `override_continue`; `finalize_cross_run`
consumes the result to decide the terminal status and does not run
delivery itself.

The loop reuses the mono primitives unchanged — `resolve_commit_delivery`
+ `apply_commit_delivery` (the latter already owns the `target_dirty`
pause/retry loop). Cross only orchestrates per-alias calls against
alias-scoped inputs:

- target checkout: `projects[alias]`;
- source worktree: child session
  `session["phases"]["projects"][alias]["worktree"]["path"]`;
- alias run_dir: the existing `<cross_run_dir>/<alias>/` child artifact
  dir (audit json lands beside the child's `diff.patch`);
- baseline_ref: child `pre_run_dirty.seed_tree_sha` → child
  `worktree.base_ref` → `HEAD`.

### Multi-alias policy

Per-alias failures are recorded and the loop continues — one dirty or
failed project never blocks delivery to the clean ones. There is no
global rollback (each repo is autonomous). A single operator `halt`
ends the loop immediately.

### Outcome matrix → terminal status

| Per-alias status | Class |
| --- | --- |
| `committed`, `applied_uncommitted`, `no_diff`, `skipped`, `skipped_already_delivered` | success-like |
| `target_dirty`, `commit_failed`, `apply_failed`, `not_applicable` | failure-like |
| `disabled` | success-like **only** when `disabled_by_config` is true |
| `halted` | stops the loop |

A child with no isolated worktree (plan-only / review-only projection)
has nothing to transport — classified success-like (`no_diff`), never
the P0.

| Delivery aggregate | `meta.status` | `meta.halt_reason` |
| --- | --- | --- |
| `ok` / `disabled` | `done` | — |
| `partial` | `failed` | `cross_delivery_partial` |
| `failed` | `failed` | `cross_delivery_failed` |
| `halted` | `halted` | `phase_handoff_halt` |

No new top-level status taxonomy is introduced (would touch CLI exit
codes, SDK surface, MCP, UI status maps, and many tests). The existing
`failed` status carries the new `halt_reason` discriminators.

### Override path (invariant 6)

`resolve_commit_delivery` blocks delivery when the child
`phases.final_acceptance.verdict != APPROVED`. On a CFA override-continue
the operator has explicitly chosen to ship the bundle, so the per-alias
gate must also pass. Per ADR 0047 invariant 5/6 option (a): the loop
passes a **synthetic** alias session whose verdict reads APPROVED, and
preserves the original reviewer verdict in
`session["phases"]["cross_delivery"][alias].release_override`
(`{original_verdict, effective_verdict: "APPROVED_FOR_DELIVERY",
source: "operator_override"}`). The mono `resolve_commit_delivery` API is
untouched, and the persisted child verdict is never rewritten.

### Evidence + idempotent resume

Per-alias detail lives in the single phase-scoped location
`session["phases"]["cross_delivery"]` (never top-level). Idempotent
resume state lives on the cross checkpoint as
`cross_checkpoint.delivery_status = {alias: {status, commit_sha}}`; a
resumed run skips aliases already delivered on a prior attempt so partial
delivery never re-commits.

### Events

Four `cross.delivery.*` event kinds (`started`, `alias_committed`,
`alias_failed`, `completed`) join the typed vocabulary in
`core/observability/event_kinds.py`. All fire strictly before `run.end`.

## Consequences

- APPROVED (and operator-overridden) cross runs now ship real commits
  into every project checkout — the P0 is closed.
- The cross terminal renderer gains a `Delivery` block and a `HALTED`
  banner; partial / failed / halted runs also render the recovery hint.
- Wire-format additions (events, session shape, halt_reasons) ship with
  a matching `orcho-mcp` read-path smoke in the same commit.

## Scope / follow-ups

- `retry_feedback` remains A2c-only / dormant — not built here.
- **Apply-before-gates** (transporting per-alias worktree diffs into the
  source checkouts *before* `contract_check` + CFA so the cross gates
  review the real changes rather than stale source) is a deliberate
  follow-up. Today the cross gates read source; a false REJECT is
  resolved by the operator override path, which still ships the real
  worktree fix because delivery reads from the worktree.
- Threading `alias` into the mono per-decision artifact json (P1.2) is
  deferred; the alias is already implicit in the
  `<cross_run_dir>/<alias>/commit_decisions/` path and explicit in the
  phase-scoped evidence keyed by alias.
