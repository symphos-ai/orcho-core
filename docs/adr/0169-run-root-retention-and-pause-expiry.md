# ADR 0169 — Run-root retention and pause expiry

- **Status:** Accepted
- **Date:** 2026-07-30
- **Extends:** [ADR 0167](0167-retention-enforcement-and-workspace-cleanup.md)

## Context

ADR 0167 established a value-based predicate for physical retained checkouts.
Run roots need a separate lifecycle and age decision: a root can be safely
removed when its checkout was never created, is gone, or was already
reclaimed. Treating those inert states as a checkout selection would weaken
the checkout safety contract, while retaining every expired stopped root would
leave durable runspace debris indefinitely.

## Decision

Workspace cleanup has two predicates. The existing worktree-tier predicate
continues to decide only whether a physical checkout may be reclaimed. A new
run-root predicate consumes normalized root, metadata, and checkout facts. It
selects a root only when it is a safe direct child of `runs/`, is stopped, its
root deadline is expired, and every dependent physical checkout group is
either inert or selected. It never makes a protected checkout reclaimable.

`worktree.retention_until`, when present, is the authoritative absolute UTC
deadline for a mono root. A deadline strictly later than `now` protects it;
one equal to or earlier than `now` is expired. A missing stamp alone permits
the legacy fallback: parse the `YYYYMMDD_HHMMSS` UTC prefix of the root id and
add the CLI `--older-than` duration (30 days by default). A malformed present
stamp, malformed id, unreadable metadata, or unsafe root path protects the
root. Directory mtime and `phase_handoff.requested_at` are not age inputs.

An open canonical phase-handoff pause is not permanent retention. An expired
`awaiting_phase_handoff` root, and the matching `interrupted` plus active
handoff shape, is treated as stopped when its checkout dependencies are inert.
An active gate, checkpoint-only handoff, live or unknown status, and all
existing checkout protections still block cleanup.

`--reclaim-worktrees` remains checkout-only. `--reclaim-both` is the explicit
authority for roots. It first archives/removes every selected physical checkout
group and writes its usual marker; an eligible root with no dependency group
may proceed after that worktree phase, while any nonempty group must succeed
before its root may proceed. Shared groups apply their result to every
dependent root. Before archive/delete, the engine re-reads each candidate and
applies the same root predicate; changed lifecycle state, handoff/gate,
checkout references, or path safety produces `changed_before_execution` rather
than root removal.

Each root archive precedes root removal. Cleanup receipts retain checkout
selection/results/byte fields and add `root_selected`, `root_protected`, and
`run_archive_snapshot` / `run_root_remove` operations; partial failures remain
durable alongside successful independent roots.

## Consequences and invariants

- Report and execution use the same root predicate. Report mode writes no
  receipt, archive, or metadata.
- The worktree predicate, including its own `retention_until` behavior, is not
  reinterpreted by the root cutoff.
- Pending-decision and `decide` projections are not edited. A queue entry can
  disappear only as the consequence of deleting its selected run directory.
- Existing artifacts are not rewritten; only new cleanup receipts and normal
  reclamation markers are written.

## Rejected alternatives

### Directory mtime as a fallback

Mtime changes during copying, repair, extraction, and operator inspection; it
is not a durable lifecycle deadline and would make report and execution
machine-dependent.

### A separate root-retention configuration source

Adding another workspace policy would create divergent clocks and unclear
operator intent. The explicit CLI cutoff is used only for the documented
legacy root-id fallback; readable durable deadlines keep precedence.

### Extending the checkout predicate to roots

This would conflate physical value protection with run lifecycle retention and
could allow inert-root policy to weaken protections for a real checkout.

## References

- [ADR 0167](0167-retention-enforcement-and-workspace-cleanup.md)
- [Workspace guide](../user/03_workspaces.md)
- [Run artifacts reference](../reference/run_artifacts.md)
