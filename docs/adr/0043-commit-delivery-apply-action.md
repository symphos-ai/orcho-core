# ADR 0043 — Commit Delivery Apply Action

- **Status:** Accepted
- **Date:** 2026-05-25
- **Deciders:** project owner
- **Extends:** [ADR 0032](0032-commit-decision-gate.md),
  [ADR 0033](0033-worktree-foundation.md)

## Context

ADR 0032 introduced the post-release commit-decision gate with three
operator actions: `approve`, `skip`, and `halt`. ADR 0033 then moved
agent-authored changes into a per-run isolated worktree by default.

That combination left an unsafe semantic gap for a common delivery
workflow: "apply several Orcho runs, then create one human-authored
commit". With per-run worktree isolation, `skip` cannot mean "I will
batch this into a later commit" because the diff remains only in the
retained run worktree and artifacts. Retention makes recovery possible,
but it is not delivery into the project's working checkout.

## Decision

Add a fourth commit-decision action: `apply`.

The post-release commit-decision actions are:

| Action | Meaning |
| --- | --- |
| `approve` | Apply the run-owned diff to the project checkout, stage the selected files, and create a commit. |
| `apply` | Apply the run-owned diff to the project checkout, but leave the result uncommitted for a later operator-owned commit. |
| `skip` | Do not deliver the run-owned diff to the project checkout. Retained artifacts remain available for manual recovery. |
| `halt` | Mark the run halted at the delivery gate. No delivery and no commit. |

The decision artifact status vocabulary gains:

| Status | Action | Meaning |
| --- | --- | --- |
| `applied_uncommitted` | `apply` | The diff was delivered to the project checkout and left uncommitted. |
| `apply_failed` | `apply` | The diff could not be delivered cleanly; the run worktree and artifacts remain the recovery source. |

Existing statuses keep their meaning:

| Status | Action | Meaning |
| --- | --- | --- |
| `committed` | `approve` | Delivery and commit succeeded. |
| `commit_failed` | `approve` | Delivery or commit failed; details live in `commit_error`. |
| `skipped` | `skip` | The operator explicitly declined delivery. |
| `halted` | `halt` | The run was halted at the delivery gate. |

## Delivery Invariants

1. `skip` is not a batching mechanism. It means no delivery.
2. Batch commits use `apply`, not `skip`.
3. A successful `apply` must leave the canonical project checkout dirty
   with the run-owned diff, and must not create a commit.
4. A failed `apply` must not delete the run worktree or the durable diff
   artifacts.
5. Worktree garbage collection must not remove the only recovery copy of
   a pending or failed delivery.

## Sequential Multi-Run Changes

`apply` is a delivery action, not a base-selection strategy. In the
default per-run isolation mode, each new run worktree starts from the
project `HEAD`; uncommitted changes in the project checkout are not
copied into the next run worktree by Git.

Therefore, dependent multi-run changes need an explicit intake strategy:

1. **Pre-run dirty intake:** when the project checkout is dirty before a
   new isolated run, Orcho asks whether to seed the run worktree with the
   current uncommitted diff, start from `HEAD`, commit first, or halt.
2. **Git-native series:** use `approve` between runs so each run advances
   `HEAD`; squash or edit commits later with normal Git tooling when a
   single final commit is desired.
3. **Isolation off:** an operator can deliberately run against the dirty
   project checkout, accepting the weaker isolation model.

The commit-decision gate must not pretend that `apply` makes the next
isolated run see the applied diff. That behavior belongs to the next
run's pre-run intake decision.

## Consequences

The public commit-decision wire surface now has four actions. CLI, SDK,
MCP, and UI clients must present `apply` separately from `skip`.

The delivery executor remains responsible for the actual sync-back
operation. This ADR only fixes the contract vocabulary so implementation
work cannot collapse "deliver uncommitted" and "do not deliver" into the
same operator action.
