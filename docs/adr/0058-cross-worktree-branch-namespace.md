# ADR 0058 — Per-cross-run worktree branch namespace (fix silent in-place degrade)

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0033](0033-worktree-foundation.md),
  [ADR 0047](0047-cross-project-application-boundary.md),
  [ADR 0049](0049-cross-level-commit-delivery.md)

## Context

Cross-project child sub-pipelines silently lost worktree isolation on the
**second and later** cross run in a workspace — the implement/repair/review
agents ran in the user's **source** checkout instead of an isolated
worktree. Verified end-to-end:

1. The cross dispatch launches each child with `resume_from=alias`
   (`project_dispatch.py`), so the child's `run_id` resolves to the bare
   alias (`bootstrap.py`: `run_id = resume_from or …`).
2. `resolve_worktree_for_run` derived **both** the checkout id and the git
   branch from `run_id`: `worktree_id = wt_<run_id>` and
   `branch = orcho/run/<run_id>` (`worktree.py`).
3. The checkout **path** is unique per cross run (it sits under the
   timestamped `runs/<cross_ts>/…/worktrees/`), but the **branch**
   `orcho/run/<alias>` is stable across cross runs and lives in the
   **shared source-repo ref namespace**.
4. The first cross run creates `orcho/run/<alias>`. Every later cross run
   runs `git worktree add -b orcho/run/<alias> <new path>`, which fails
   with *"branch already in use"*. `resolve_worktree_for_run` then
   **silently** falls back to `_off_context` (in-place, `cwd = source`).

Confirmed on a live demo workspace: `git branch --list 'orcho/run/*'`
showed `orcho/run/web` / `orcho/run/api` left over from a prior cross run,
and the next run's child prompt carried the in-place project-context block
(`cwd = <source>/web`) instead of the worktree one.

Impact is high and silent: the agent edits the user's tree; the cross
gates (`review_changes`, `contract_check`, CFA) then `git diff` the empty
worktree; cross delivery has nothing to transport. The only trace is a
`degraded_reason` buried in the worktree block.

## Decision

**A — decouple the branch name from `run_id`.** `resolve_worktree_for_run`
gains a `branch_run_id` parameter: `branch = orcho/run/<branch_run_id or
run_id>`. `worktree_id` (and therefore the `wt_<alias>` checkout path that
finalization / cross delivery depend on) stays keyed on `run_id`. Cross
children pass `branch_run_id = "<parent_run_id>__<alias>"`
(`pipeline/project/app.py`), so each cross run gets a fresh, collision-free
branch while the path contract is unchanged. Mono runs pass nothing
(`branch_run_id=None`) → branch stays `orcho/run/<timestamp>`, byte-identical.

**B — cross worktree degrade is loud, never silent.** A cross child
(`parent_run_id` set) whose isolation was *requested but degraded* (the
context carries a `degraded_reason`) now raises `WorktreeConfigError`
instead of running in-place. The cross dispatch already catches child
exceptions and records the alias as `failed`, so a degrade surfaces as a
visible failed alias rather than silently shipped source edits. An explicit
`worktree.enabled=false` is a clean off (no `degraded_reason`) and remains
the operator's choice.

## Why not the alternatives

- **Change the child `run_id` to be unique per cross run.** Rejected: it
  also moves the checkout id/path (`wt_<run_id>`), breaking the
  `<cross_run_dir>/worktrees/wt_<alias>` contract that finalization and
  `cross_delivery` resolve by alias.
- **Reuse the leftover branch/worktree on collision.** Rejected: it would
  silently fold a prior unrelated cross run's state into the new run.
- **Prune `orcho/run/<alias>` after every cross run.** Rejected: fragile,
  and it fights the retention model; the branch name should just be unique.

## Consequences

- Repeated cross runs in the same workspace keep isolation; the second run
  no longer edits source.
- Leftover orcho branches still accumulate one-per-(cross-run, alias) — the
  same property mono already has (one-per-run). Cleanup/retention is a
  separate concern, unchanged here.
- Mono behaviour is unchanged (no `branch_run_id`).
- A genuinely degraded cross child now fails loudly (alias `failed`) — a
  behaviour change from the previous silent in-place run, and the intended
  safety improvement.

## Verification

- `tests/unit/pipeline/engine/test_worktree_resolution.py::TestBranchRunIdDecoupling`
  — branch override keeps the `wt_<alias>` path; two cross runs with the
  same alias `run_id` but distinct `branch_run_id` both stay isolated; and a
  guard test reproduces the original collision (stable branch → degrade).
- Worktree + cross_project + project_run + git_worktree suites green;
  full suite + `orcho-mcp` mock smoke green.
