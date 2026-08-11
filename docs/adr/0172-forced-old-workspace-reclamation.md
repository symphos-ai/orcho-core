# ADR 0172 — Forced old workspace reclamation

- **Status:** Accepted
- **Date:** 2026-07-30
- **Extends:** [ADR 0167](0167-retention-enforcement-and-workspace-cleanup.md), [ADR 0169](0169-run-root-retention-and-pause-expiry.md), [ADR 0170](0170-public-sdk-workspace-cleanup.md), and [ADR 0171](0171-retention-aware-checkout-handoff-protection.md)

## Context

Normal cleanup protects durable value and unresolved coordination. An operator
occasionally needs to reclaim an abandoned workspace despite a small, known set
of value protections. That authority must not turn unreadable state, a live
run, or an unsafe path into permission to delete it. It must also not mistake a
retention deadline for proof that the workspace itself is old.

## Decision

`force=True` is a narrow engine predicate, owned by
[`pipeline/engine/workspace_run_retention.py`](../../pipeline/engine/workspace_run_retention.py)
and applied by the single selector in
[`pipeline/engine/workspace_cleanup.py`](../../pipeline/engine/workspace_cleanup.py).
It can change only these protected reasons into selected reasons:

- `uncommitted_changes` → `forced_reclaim_uncommitted_changes`
- `unpushed_commits` → `forced_reclaim_unpushed_commits`
- `active_handoff_or_gate` → `forced_reclaim_active_handoff_or_gate`
- `checkpoint_handoff_active` → `forced_reclaim_checkpoint_handoff_active`

The override requires a caller-supplied `older_than`. Its age proof parses only
the `YYYYMMDD_HHMMSS` UTC prefix of the root run id, adds `older_than`, and
requires that instant to be **strictly earlier** than `now`. Equality does not
qualify. A missing or malformed prefix is unknown age and remains protected;
`retention_until`, directory mtime, and handoff timestamps are not force-age
inputs.

All structural protections remain fail-closed, including live or unknown run
state, unreadable metadata or Git state, unsafe or symlink paths, unknown age,
shared checkouts with a protected reference, and live or paused cross parents.
The cross-parent guard is structural even when its child would otherwise have
an allowlisted reason.

Report and execution continue to call one selector. Immediately before each
checkout group or root mutation, execution re-applies the same `force`, cutoff,
and structural guards through group/root revalidation. A changed candidate is
recorded as `changed_before_execution`; no force-specific candidate list or
execution loop exists.

CLI force is reclaim-only: `--force` requires both `--older-than DAYS` and
`--reclaim-worktrees` or `--reclaim-both`. The ordinary reclaim disposition is
`archive`, which creates a recoverable archive snapshot. `--force --delete` is
an explicit irreversible deletion request. The SDK separately permits a
read-only forced selection preview through `report_workspace_cleanup`; the
engine is the deep validation boundary for a missing cutoff.

Cleanup receipts keep their schema version and existing selection, operation,
and byte-accounting fields. They add `force` and normalized `force_cutoff`; the
selected and root-selected entries carry the explicit `forced_reclaim_*`
reasons when an override applied.

## Consequences and invariants

- Non-force selection, reason ordering, and normal retention semantics remain
  unchanged.
- The public Python calls in [`sdk/cleanup.py`](../../sdk/cleanup.py) forward
  `force` to the engine; the thin CLI adapter in
  [`cli/_workspace_cleanup.py`](../../cli/_workspace_cleanup.py) validates the
  CLI-only authority shape and renders force intent.
- This does not add an MCP tool, resource, wire payload, or schema surface.
- Archive/delete routing and durable receipt ordering remain owned by the
  cleanup engine.

## References

- [Workspace cleanup guide](../user/03_workspaces.md#reclaiming-expired-retained-worktrees)
- [Run artifacts reference](../reference/run_artifacts.md#retention-cleanup-artifacts)
- [SDK API reference](../reference/sdk_api.md#sdkcleanup)
