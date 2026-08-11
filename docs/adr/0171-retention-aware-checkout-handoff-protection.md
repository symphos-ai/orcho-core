# ADR 0171 — Retention-aware checkout handoff protection

- **Status:** Accepted
- **Date:** 2026-07-30
- **Extends:** [ADR 0169](0169-run-root-retention-and-pause-expiry.md)

## Context

ADR 0169 made phase-handoff retention at the run-root level time-bounded. A
retained checkout uses the same run lifecycle, but is the physical location
that may still contain unrecoverable work. Leaving an open handoff or gate as
an unconditional checkout protection would make an abandoned paused checkout
immortal; using a different age calculation at this layer would let root and
checkout cleanup disagree.

The cleanup report, execution receipt, SDK, and CLI must therefore describe
the same expiry decision without turning a cleanup action into an operator
decision or changing artifacts that record an unresolved decision.

## Decision

Checkout cleanup uses the same single deadline resolver as run-root cleanup.
For a retained checkout, a readable `worktree.retention_until` is the
authoritative absolute UTC deadline. Only when that field is absent does the
resolver parse the `YYYYMMDD_HHMMSS` UTC prefix of the root run id and add the
configured `--older-than` duration. A present malformed deadline, malformed
root id, unreadable metadata, or unsafe path fails closed. Filesystem and
directory mtime are never age inputs.

An open canonical phase handoff protects a stopped checkout only inside that
retention window. At or after expiry, an eligible paused checkout is selected
with `pause_retention_expired`; an ordinary stopped checkout uses
`retention_expired`. This is a retention decision, not an automatic handoff
decision: cleanup does not continue, resolve, waive, or otherwise decide a
handoff.

A real active gate is different from a stale handoff. A cross gate — a
`pending_gate` record or a live cross-checkpoint — is a live coordination
point, not lost pause state, so it stays fail-closed regardless of age, exactly
as in ADR 0169. Only an open phase handoff is retention-bounded; a genuine gate
is never released by expiry.

Expiry never weakens value or safety protections. Dirty worktrees, unpushed
commits, live or unknown lifecycle state, active checkpoint-only handoffs,
active cross gates, unsafe paths, unreadable metadata or Git state, and
protected shared checkout references remain protected. A root remains independently eligible only when
its own retention/lifecycle predicate passes and every dependent checkout is
inert or selected, as specified by ADR 0169.

Before archive or deletion, the engine re-reads the planned checkout or root
and reapplies the same predicate. A changed deadline, lifecycle, handoff/gate,
checkout reference, safety fact, or value probe prevents mutation and is
reported as `changed_before_execution` where applicable. Cleanup receipts
record the original selection and root summaries, including expiry reasons;
pending-decision and `decide` artifacts are never rewritten.

## Consequences and invariants

- Report and execution share the resolver and predicate; a report is not an
  executable authorization because execution re-verifies immediately before
  mutation.
- `--older-than` retains its existing default and affects only the legacy,
  absent-stamp root-id fallback. It neither overrides a durable deadline nor
  supplies a force override.
- `pause_retention_expired` is visible in checkout `selected`/`protected` and
  run-root `root_selected`/`root_protected` receipt summaries, so report and
  receipt consumers can distinguish expiry from a normal stopped-run expiry.
- Cleanup may remove a selected run directory, which can make its projected
  queue entry disappear, but it does not edit pending-decision artifacts or
  make an automatic operator decision.
- Archive/delete routing, durable receipt writes, and the dirty, unpushed,
  live, unsafe, and unreadable protections from ADRs 0167 and 0169 remain
  unchanged.

## References

- [ADR 0169](0169-run-root-retention-and-pause-expiry.md)
- [ADR 0167](0167-retention-enforcement-and-workspace-cleanup.md)
- [Workspace cleanup guide](../user/03_workspaces.md#reclaiming-expired-retained-worktrees)
- [Run artifacts reference](../reference/run_artifacts.md#retention-cleanup-artifacts)
