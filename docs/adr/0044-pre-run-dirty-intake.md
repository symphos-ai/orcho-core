# ADR 0044 — Pre-Run Dirty Intake

- **Status:** Accepted
- **Date:** 2026-05-25
- **Deciders:** project owner
- **Extends:** [ADR 0033](0033-worktree-foundation.md),
  [ADR 0043](0043-commit-delivery-apply-action.md)

## Context

Per-run worktree isolation protects the project checkout by creating each
physical checkout under `<workspace>/runspace/worktrees/<worktree_id>/checkout/`
from the project `HEAD`. That is safe, but
it has one surprising edge: uncommitted changes in the project checkout do
not automatically appear in a new isolated run.

ADR 0043 adds `apply`, which delivers a completed run's diff back into the
project checkout without committing it. That supports human-owned batch
commits, but it also creates a natural follow-up question: if the operator
starts another run while the project checkout is dirty, should the new run
continue from those uncommitted changes?

A warning-only gate would be too weak. Orcho can do better: show the dirty
state and ask how to intake it into the next run.

## Decision

Add a pre-run dirty intake gate before creating a per-run isolated
worktree. The gate fires when all are true:

1. worktree isolation is enabled for the run;
2. the project checkout has tracked or selected untracked changes;
3. the run would otherwise create a clean worktree from `HEAD`.

The gate offers four actions:

| Action | Meaning |
| --- | --- |
| `include` | Seed the new run worktree with the current project checkout diff, then run there. |
| `exclude` | Start the run from `HEAD`; leave project checkout changes untouched. |
| `commit` | Commit the current project checkout first, then start the run from the new `HEAD`. |
| `halt` | Stop before creating the run worktree. |

The operator must be shown at least a diff stat and changed-path list before
choosing. Full diff preview may be capped for UI readability, but the
decision must be based on a durable snapshot rather than a transient shell
view.

## Include Semantics

`include` is a seed operation, not a checkout copy.

1. Snapshot the project checkout diff, including configured untracked files.
2. Create the run worktree from `HEAD`.
3. Run `git apply --check` inside the run worktree.
4. Apply the snapshot inside the run worktree.
5. Record the seed metadata in run meta/evidence:
   source checkout path, source `HEAD`, dirty file list, untracked list,
   and snapshot artifact path.
6. Leave the project checkout untouched.

If the snapshot cannot apply cleanly, the run must not start silently from
`HEAD`. The intake gate records the failure and asks for a new action or
halts, depending on the caller surface.

## Defaults

Interactive default:

- Preselect `include` when the dirty diff is non-empty and applies cleanly,
  because it matches the common "continue from my current working version"
  expectation.
- Still require explicit confirmation; no silent intake.

Non-interactive default:

- `halt`, unless config explicitly sets a different policy.

Rationale: background runs must not silently absorb or ignore local dirty
state.

## Config Shape

The default config should add a `pre_run_dirty` block:

```json
{
  "pre_run_dirty": {
    "enabled": true,
    "interactive_default": "include",
    "non_interactive_default": "halt",
    "include_untracked": "prompt"
  }
}
```

`include_untracked` values:

| Value | Meaning |
| --- | --- |
| `prompt` | Ask interactively which untracked files to seed. |
| `all` | Include all untracked files not ignored by Git. |
| `none` | Never include untracked files in the seed snapshot. |

## Relationship To Commit Delivery

Pre-run dirty intake is the bridge for sequential multi-run changes:

```text
run 1 -> apply -> project checkout has uncommitted diff
run 2 start -> pre-run dirty intake -> include
run 2 worktree starts with run 1 diff seeded
```

The commit-decision gate remains responsible for delivery after a run.
The pre-run dirty gate is responsible for selecting the input state before
a run.

## Consequences

This avoids a heavier change-lane abstraction in the first implementation.
It also keeps the operator in control of dirty state without reducing the
protection provided by per-run worktree isolation.

Future work may still add named change lanes if Orcho needs durable
multi-run integration branches, but that is not required to make sequential
runs understandable and safe.
