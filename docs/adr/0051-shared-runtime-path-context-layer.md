# ADR 0051 — Shared runtime path/context layer (cross ↔ mono DRY)

- **Status:** Proposed
- **Date:** 2026-05-28
- **Deciders:** project owner
- **Relates to:** [ADR 0047](0047-cross-project-application-boundary.md),
  [ADR 0049](0049-cross-level-commit-delivery.md),
  [ADR 0050](0050-structured-cross-handoff.md)

## Context

The cross-project pipeline (`pipeline/cross_project/`) maintains
**parallel reimplementations** of machinery the single-project (mono)
pipeline (`pipeline/project/`, `pipeline/phases/`) already owns. The
duplication is the root cause behind several recent cross-pipeline bugs,
each of which was a place where cross diverged from mono's already-
correct behaviour:

- **Path binding.** Mono resolves the agent cwd through
  `_agent_project_dir(state)` = `extras["git_cwd"]` (the worktree when
  isolated) and a single project-context block. Cross instead threaded
  SOURCE paths into gate prompts and the handoff:
  - gates (`contract_check` + CFA) reviewed the SOURCE checkout, not the
    per-alias worktree where the change lives → false REJECTs (fixed in
    `c732010` by pointing the gates at the worktrees).
  - the handoff echoed source `project_path` into the implement prompt
    (fixed in `7300768`).
- **Delivery.** Cross-level delivery (`ADR 0049`) deliberately reused the mono
  `resolve_commit_delivery` / `apply_commit_delivery` primitives rather
  than re-implementing transport — proof the shared-primitive approach
  works and should be the norm, not the exception.
- **Finalization, gate wiring, runtime context** are likewise
  cross-private copies that drift from mono.

## Decision (proposed)

Extract a **shared runtime/path/context layer** that both mono and
cross consume, so cross stops carrying its own copy of:

- agent cwd / worktree resolution (the `git_cwd` + project-context-block
  contract);
- change-surface review targeting (review_uncommitted at the worktree);
- delivery orchestration (already shared via the engine primitives —
  formalize the boundary);
- finalization status/terminal-event ownership.

Cross becomes a thin orchestration layer over the shared execution
contracts; per-alias child runs already route through
`run_project_pipeline` (a good precedent) — extend that "cross is
mono-N-times over a shared core" principle to the path/context/gate
surfaces.

## Known edge to fold in

- **isolation-off `target_dirty`.** When worktree isolation degrades to
  in-place (`worktree.path == source`), cross delivery sees the run's
  own in-place changes as a dirty target and blocks the commit
  (`target_dirty`). Per owner decision this should follow mono's
  **interactive** target_dirty contract (prompt retry/skip/halt; headless
  records and continues) for parity — NOT a silent in-place commit. A
  shared delivery/path layer is the natural home for that parity.

## Scope / non-goals

- This is an execution-layer reshape, NOT a bug fix — high blast radius,
  its own design + migration plan. Do not bundle into bug-fix diffs.
- Sequencing: land [ADR 0050](0050-structured-cross-handoff.md)
  (structured handoff) first; it is a smaller, self-contained step
  toward the shared contract.

## Open questions

- Where does the shared layer live so `orcho-core` keeps mono importable
  without a cross dependency (direction rule: cross may depend on mono
  internals, not vice versa)?
- How much of `pipeline/cross_project/finalization.py` collapses into the
  mono finalization service vs stays cross-specific (run.end ownership,
  per-alias rollup)?
