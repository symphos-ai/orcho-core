---
name: orcho-core-isolation-worktrees-sandbox
description: "Use when editing orcho-core isolation and workspace safety: pipeline/project/isolation_setup.py, worktree selection/bootstrap, pre-run dirty intake, sandbox policy, runspace paths, dependency checkout subjects, git worktree tests, sandbox ADRs, or generated worktree handling. Do not use for runtime agent sessions or phase lifecycle unless paired."
---

# Orcho Core Isolation Worktrees Sandbox

Own the safety boundary around checkouts, worktrees, dirty state, runspace, and
sandbox policy.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/project/isolation_setup.py`
- `orcho-core/docs/adr/0033-worktree-foundation.md`
- `orcho-core/docs/adr/0034-sandbox-isolation-layered.md`
- `orcho-core/docs/adr/0044-pre-run-dirty-intake.md`
- `orcho-core/docs/architecture/sandbox.md`
- changed worktree/sandbox modules

## Owns

- worktree selection/bootstrap and runspace paths
- pre-run dirty intake
- sandbox policy and launch-layer process hygiene
- dependency checkout subjects for verification
- generated worktree handling rules inside core

## Does Not Own

- runtime adapter sessions -> `orcho-core-runtime-session`
- phase lifecycle -> `orcho-core-phases-engine`
- verification command semantics -> `orcho-core-quality-gates` or `orcho-core-test-infra-goldens`

## Invariants

- Do not edit unrelated generated worktrees.
- Tests involving git worktrees, subprocesses, shared env, or ports are serial unless proven safe.
- Keep run worktree subject distinct from canonical project/dependency subjects.
- Never use destructive git commands unless explicitly requested.

## Verification

- From `orcho-core`: `python -m pytest -q -m worktree`
- From `orcho-core`: `python -m pytest -q -m git_worktree` when real worktrees are touched.
- From `orcho-core`: run targeted isolation/project-run tests for changed modules.

## Neighbor Skills

- `orcho-core-phases-engine` when lifecycle sequencing changes
- `orcho-core-runtime-session` when runtime cwd/session behavior changes
- `orcho-core-test-infra-goldens` when fixtures or shared test hooks change
