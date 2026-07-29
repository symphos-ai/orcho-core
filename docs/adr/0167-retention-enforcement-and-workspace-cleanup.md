# ADR 0167 — Retention enforcement and workspace cleanup

- **Status:** Accepted
- **Date:** 2026-07-29

## Decision

`orcho workspace cleanup` is report-only by default. It selects only stopped,
expired retained worktrees through one predicate shared by report and
execution. Protection is value-based: a checkout is protected only while it
holds work that cannot be recovered from anywhere else — uncommitted changes,
commits absent from every remote, a live/paused/unknown run, or an active
handoff or gate — or while its state cannot be read safely (malformed
metadata, a repository that exists but cannot answer, paths outside the
resolved runspace, symlink escapes). A checkout whose repository is gone can
hold nothing recoverable and is reclaimable. Registration and manifests are
identity bookkeeping: they select the removal route (git deregistration for a
registered worktree, plain directory removal otherwise), never eligibility.
References with no retained checkout at all are reported as inert, not
protected.

`--reclaim-worktrees` reclaims selected physical checkouts while retaining all
run directories. `--reclaim-both` may then reclaim a root run only after every
selected checkout for that root succeeded; there is no run-only operation.
Shared follow-up and embedded cross references are grouped by canonical
checkout path — a fact on disk — so one protected reference protects the
group.

Archive is the default disposition. A verified lossless snapshot is written
under `<runspace>/cleanup_archive/<receipt_id>/` before git deregistration;
`--delete` is the explicit irreversible alternative. Registered worktrees are
removed only by the engine worktree removal seam, never recursive checkout
deletion; unregistered directories have no registration to damage and are
removed by one confined helper. Archive reports `bytes_archived` and zero
reclaimed bytes; delete reports `bytes_reclaimed`.

Each execution creates a receipt in `<runspace>/cleanup_receipts/` before its
first mutation and updates it atomically after operations, including partial
failures. Successful reclamation atomically stamps every retained reference
with `worktree.reclaimed = {at, disposition, archive_path, receipt_path}`.
The original `path` remains historical evidence only and cannot be resumed or
followed up in place.
