"""Discovery and selection for retained workspace worktrees.

Selection protects value, not bookkeeping.  A checkout is kept when it holds
work that cannot be recovered from anywhere else — uncommitted changes,
commits no remote has, or a run that may still resume in place.  Registration
and manifests prove identity, so they decide *how* a checkout is removed,
never whether it may be.

Selection itself never archives or deletes anything.  Both the report surface
and the mutation surface consume :func:`select_workspace_cleanup`, which makes
it impossible for the two to drift in their safety predicate.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pipeline.engine.workspace_run_retention import (
    RETENTION_UNSET,
    RunRootFacts,
    RunRootVerdict,
    evaluate_run_root,
    forced_reclaim_reason,
    resolve_retention_deadline,
)
from pipeline.engine.worktree import is_worktree_reclaimed
from pipeline.run_state.setup_failure import merged_status
from pipeline.run_state.status_vocab import (
    INTERRUPTED_STATUS,
    PAUSE_STATUS,
    STOPPED_RETENTION_STATUSES,
)

RegistrationChecker = Callable[[Path, Path], bool]
CleanupTier = Literal["worktrees", "both"]
CleanupDisposition = Literal["archive", "delete"]


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupSnapshot:
    """One durable reference to a physical checkout, as observed on disk."""

    run_id: str
    run_dir: Path
    meta_path: Path
    worktree_path: Path | None
    meta: Mapping[str, Any] | None
    root_run_id: str
    source_repo_path: Path | None


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupVerdict:
    """A stable selection result; ``reason`` is safe for report rendering."""

    snapshot: WorkspaceCleanupSnapshot
    selected: bool
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupPlan:
    """Immutable, read-only selection output shared by report and execution.

    ``inert`` references have no retained checkout to act on (none was ever
    recorded, or it was already reclaimed); they are neither reclaimable nor
    meaningfully "protected".
    """

    runs_dir: Path
    selected: tuple[WorkspaceCleanupVerdict, ...]
    protected: tuple[WorkspaceCleanupVerdict, ...]
    inert: tuple[WorkspaceCleanupVerdict, ...]
    root_run_ids_for_both: tuple[str, ...]
    root_selected: tuple[RunRootVerdict, ...] = ()
    root_protected: tuple[RunRootVerdict, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupExecution:
    """Receipt location and final durable payload from one cleanup attempt."""

    receipt_path: Path
    receipt: Mapping[str, Any]


_INERT_REASONS = frozenset(
    {
        "worktree_missing",
        "worktree_gone",
        "already_reclaimed",
        "meta_missing",
    }
)


def select_workspace_cleanup(
    runs_dir: Path | str,
    *,
    now: datetime | None = None,
    older_than: timedelta | None = None,
    force: bool = False,
) -> WorkspaceCleanupPlan:
    """Discover retained worktrees and return one selection plan.

    ``runs_dir`` is the already-resolved runs-dir contract (not a workspace
    search).  A physical checkout is selected only once all of its references
    are individually eligible.  Registration is never consulted here: it
    influences how execution removes a checkout, not whether it may.
    """
    retention = _normalize_older_than(older_than, force=force)
    root = Path(runs_dir)
    instant = _utc_now(now)
    snapshots = _discover(root)
    individual = _evaluate_snapshots(snapshots, root, instant, retention, force)

    selected = tuple(v for v in individual if v.selected)
    inert = tuple(v for v in individual if not v.selected and v.reason in _INERT_REASONS)
    protected = tuple(v for v in individual if not v.selected and v.reason not in _INERT_REASONS)
    roots = _evaluate_roots(root, snapshots, individual, instant, retention, force)
    root_selected = tuple(v for v in roots if v.selected)
    root_protected = tuple(v for v in roots if not v.selected)
    return WorkspaceCleanupPlan(
        root,
        selected,
        protected,
        inert,
        tuple(v.facts.root_id for v in root_selected),
        root_selected,
        root_protected,
    )


def execute_workspace_cleanup(
    runs_dir: Path | str,
    *,
    tier: CleanupTier = "worktrees",
    disposition: CleanupDisposition = "archive",
    now: datetime | None = None,
    older_than: timedelta | None = None,
    force: bool = False,
    registration_checker: RegistrationChecker | None = None,
    archive_root: Path | None = None,
) -> WorkspaceCleanupExecution:
    """Execute a previously configured cleanup tier with a durable receipt.

    This function deliberately computes (and re-computes) the same selection
    predicate as reporting.  There is no run-only tier: roots are considered
    solely after every selected physical checkout for that root succeeded.
    """
    if tier not in {"worktrees", "both"}:
        raise ValueError("tier must be 'worktrees' or 'both'")
    if disposition not in {"archive", "delete"}:
        raise ValueError("disposition must be 'archive' or 'delete'")
    retention = _normalize_older_than(older_than, force=force)
    root = Path(runs_dir)
    instant = _utc_now(now)
    checker = registration_checker or _registered
    plan = select_workspace_cleanup(root, now=instant, older_than=retention, force=force)
    receipt_path = root.parent / "cleanup_receipts" / f"cleanup-{uuid.uuid4().hex}.json"
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": receipt_path.stem,
        "created_at": instant.isoformat().replace("+00:00", "Z"),
        "runs_dir": str(root),
        "tier": tier,
        "disposition": disposition,
        "force": force,
        "force_cutoff": (instant - retention).isoformat().replace("+00:00", "Z"),
        "status": "running",
        "selected": [_verdict_payload(v) for v in plan.selected],
        "protected": [_verdict_payload(v) for v in plan.protected],
        "root_selected": [_root_verdict_payload(v) for v in plan.root_selected],
        "root_protected": [_root_verdict_payload(v) for v in plan.root_protected],
        "inert": len(plan.inert),
        "results": [],
        "operations": [],
        "errors": [],
        "bytes_selected": _selected_bytes(plan.selected),
        "bytes_archived": 0,
        "bytes_reclaimed": 0,
    }
    _write_atomic(receipt_path, receipt)  # durable boundary before mutation

    groups = _selected_groups(plan.selected)
    succeeded_groups: set[Path] = set()
    failed_roots: set[str] = set()
    for physical, verdicts in groups.items():
        # State can change after reporting. Re-evaluate this physical group's
        # durable references immediately before mutation.  This applies the
        # same individual/shared/parent predicate without repeatedly scanning
        # every run or invoking git for unrelated checkouts.
        fresh_verdicts = _reverify_group(verdicts, root, instant, retention, force)
        if fresh_verdicts is None:
            receipt["protected"].append(
                {
                    "path": str(physical),
                    "reason": "changed_before_execution",
                    "detail": "selection predicate no longer permits reclamation",
                }
            )
            failed_roots.update(v.snapshot.root_run_id for v in verdicts)
            _write_atomic(receipt_path, receipt)
            continue
        result = _reclaim_group(
            physical,
            fresh_verdicts,
            root,
            disposition,
            receipt_path,
            receipt,
            archive_root,
            checker,
        )
        receipt["results"].append(result)
        if result["ok"]:
            succeeded_groups.add(physical)
            if disposition == "archive":
                receipt["bytes_archived"] += result["bytes"]
            else:
                receipt["bytes_reclaimed"] += result["bytes"]
        else:
            failed_roots.update(v.snapshot.root_run_id for v in verdicts)
            receipt["errors"].append(result["error"])
        _write_atomic(receipt_path, receipt)

    if tier == "both":
        for planned_root in plan.root_selected:
            root_id = planned_root.facts.root_id
            dependencies = {_canonical(path) for path in planned_root.facts.dependency_paths}
            if root_id in failed_roots or not dependencies.issubset(succeeded_groups):
                receipt["root_protected"].append(
                    {
                        "root_run_id": root_id,
                        "reason": "checkout_group_failed",
                        "detail": "a required checkout group was not reclaimed successfully",
                    }
                )
                _write_atomic(receipt_path, receipt)
                continue
            current = _reverify_root(planned_root, root, instant, retention, force)
            # A successfully reclaimed group is now represented as inert
            # (``already_reclaimed``), so the refreshed dependency set may
            # shrink. New selected dependencies, however, prove the root's
            # checkout references changed and must stop root removal.
            if current is None or not {
                _canonical(path) for path in current.facts.dependency_paths
            }.issubset(dependencies):
                receipt["root_protected"].append(
                    {
                        "root_run_id": root_id,
                        "reason": "changed_before_execution",
                        "detail": "root predicate or checkout dependencies changed before removal",
                    }
                )
                _write_atomic(receipt_path, receipt)
                continue
            root_result = _reclaim_run_root(
                root / root_id,
                root.parent,
                disposition,
                receipt_path,
                receipt,
                archive_root,
            )
            receipt["results"].append(root_result)
            if root_result["ok"]:
                if disposition == "archive":
                    receipt["bytes_archived"] += root_result["bytes"]
                else:
                    receipt["bytes_reclaimed"] += root_result["bytes"]
            else:
                receipt["errors"].append(root_result["error"])
            _write_atomic(receipt_path, receipt)
    receipt["status"] = "complete" if not receipt["errors"] else "partial"
    _write_atomic(receipt_path, receipt)
    return WorkspaceCleanupExecution(receipt_path, receipt)


def _discover(runs_dir: Path) -> list[WorkspaceCleanupSnapshot]:
    """Read only direct root runs and their explicitly declared cross children."""
    try:
        roots = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    except OSError:
        return [
            WorkspaceCleanupSnapshot(
                "<runs-dir>",
                runs_dir,
                runs_dir / "meta.json",
                None,
                None,
                "<runs-dir>",
                None,
            )
        ]
    snapshots: list[WorkspaceCleanupSnapshot] = []
    for run_dir in roots:
        snapshots.extend(_discover_root(run_dir, run_dir.name))
    return snapshots


def _discover_root(run_dir: Path, root_id: str) -> list[WorkspaceCleanupSnapshot]:
    """Read one root and its declared children for narrow root revalidation."""
    meta = _read_object(run_dir / "meta.json")
    snapshots = [_snapshot(root_id, run_dir, meta, root_id)]
    if meta is None:
        return snapshots
    for alias in _declared_aliases(meta):
        child_dir = run_dir / alias
        # Direct child ownership forbids aliases such as ../other-run.
        if child_dir.parent != run_dir or not child_dir.is_dir():
            snapshots.append(
                WorkspaceCleanupSnapshot(
                    alias,
                    child_dir,
                    child_dir / "meta.json",
                    None,
                    None,
                    root_id,
                    None,
                )
            )
            embedded = _embedded_child(meta, alias)
            if embedded is not None:
                snapshots.append(
                    _snapshot(
                        alias,
                        child_dir,
                        embedded,
                        root_id,
                        meta_path=run_dir / "meta.json",
                    )
                )
            continue
        child_meta = _read_object(child_dir / "meta.json")
        snapshots.append(_snapshot(alias, child_dir, child_meta, root_id))
        embedded = _embedded_child(meta, alias)
        if embedded is not None:
            # The embedded session is a separate durable reference.  It
            # cannot overwrite the child file: disagreement protects the
            # physical checkout through the normal shared-path grouping.
            snapshots.append(
                _snapshot(
                    alias,
                    child_dir,
                    embedded,
                    root_id,
                    meta_path=run_dir / "meta.json",
                )
            )
    return snapshots


def _evaluate_snapshots(
    snapshots: list[WorkspaceCleanupSnapshot],
    runs_dir: Path,
    now: datetime,
    older_than: timedelta,
    force: bool,
) -> list[WorkspaceCleanupVerdict]:
    """Apply the shared individual, parent, and shared-checkout predicate."""
    individual = [
        _individual_verdict(snapshot, runs_dir, now, older_than) for snapshot in snapshots
    ]
    parent_guards = {
        snapshot.root_run_id: _cross_parent_protection(snapshot, now, older_than, force)
        for snapshot in snapshots
        if snapshot.run_id == snapshot.root_run_id
        and snapshot.meta is not None
        and _declared_aliases(snapshot.meta)
    }
    for index, verdict in enumerate(individual):
        parent_guard = parent_guards.get(verdict.snapshot.root_run_id)
        if (
            parent_guard is not None
            and verdict.snapshot.run_id != verdict.snapshot.root_run_id
            and verdict.reason not in _INERT_REASONS
        ):
            individual[index] = _protected(verdict.snapshot, *parent_guard)

    individual = [_force_verdict(verdict, now, older_than, force) for verdict in individual]

    by_checkout: dict[Path, list[int]] = {}
    for index, verdict in enumerate(individual):
        path = verdict.snapshot.worktree_path
        if path is not None:
            by_checkout.setdefault(_canonical(path), []).append(index)
    for indexes in by_checkout.values():
        # One physical checkout, one verdict: if any run sharing it is
        # protected, the checkout stays. Grouping is by canonical path, which
        # is a fact on disk — it needs no manifest to be true.
        if any(not individual[index].selected for index in indexes):
            for index in indexes:
                if individual[index].selected:
                    old = individual[index]
                    individual[index] = WorkspaceCleanupVerdict(
                        old.snapshot,
                        False,
                        "shared_checkout_protected",
                        "another run still protects this shared physical checkout",
                    )
    return individual


def _evaluate_roots(
    runs_dir: Path,
    snapshots: list[WorkspaceCleanupSnapshot],
    individual: list[WorkspaceCleanupVerdict],
    now: datetime,
    older_than: timedelta,
    force: bool,
) -> list[RunRootVerdict]:
    """Project checkout facts into the independent run-root predicate."""
    root_snapshots = {
        snapshot.root_run_id: snapshot
        for snapshot in snapshots
        if snapshot.run_id == snapshot.root_run_id
    }
    by_root: dict[str, list[WorkspaceCleanupVerdict]] = {}
    for verdict in individual:
        by_root.setdefault(verdict.snapshot.root_run_id, []).append(verdict)
    verdicts: list[RunRootVerdict] = []
    for root_id, snapshot in sorted(root_snapshots.items()):
        if snapshot.run_dir.parent != runs_dir:
            continue
        meta = snapshot.meta
        worktree = meta.get("worktree") if isinstance(meta, Mapping) else None
        retention = (
            worktree.get("retention_until", RETENTION_UNSET)
            if isinstance(worktree, Mapping)
            else RETENTION_UNSET
        )
        try:
            status = merged_status(dict(meta), snapshot.run_dir) if meta is not None else None
        except Exception:
            status = None
        checkpoint = _read_object(snapshot.run_dir / "cross_checkpoint.json")
        active_handoff = _active_handoff(meta) if isinstance(meta, Mapping) else False
        checkpoint_handoff = _checkpoint_only_handoff(checkpoint, status, active_handoff)
        active_gate = bool(
            (
                isinstance(meta, Mapping)
                and isinstance(meta.get("pending_gate"), Mapping)
                and meta["pending_gate"]
            )
            or (checkpoint and checkpoint.get("pending_gate"))
        )
        entries = by_root.get(root_id, [])
        if isinstance(meta, Mapping) and _declared_aliases(meta):
            # A cross parent supplies lifecycle state; its declared children
            # own the physical checkout dependencies.
            entries = [entry for entry in entries if entry.snapshot.run_id != root_id]
        blocker = next(
            (
                entry.reason
                for entry in entries
                if not entry.selected and entry.reason not in _INERT_REASONS
            ),
            None,
        )
        dependencies = tuple(
            sorted(
                {
                    _canonical(entry.snapshot.worktree_path)
                    for entry in entries
                    if entry.selected and entry.snapshot.worktree_path is not None
                },
                key=str,
            )
        )
        facts = RunRootFacts(
            root_id=root_id,
            run_dir=snapshot.run_dir,
            meta=meta,
            meta_exists=snapshot.meta_path.exists(),
            status=status,
            retention_until=retention,
            active_handoff=active_handoff,
            checkpoint_handoff=checkpoint_handoff,
            active_gate=active_gate,
            path_safe=_safe_run_root(snapshot.run_dir, runs_dir),
            nested_checkout_unidentified=_has_unidentified_nested_checkout(
                snapshot.run_dir, entries
            ),
            checkout_blocker=blocker,
            dependency_paths=dependencies,
        )
        verdicts.append(evaluate_run_root(facts, now=now, older_than=older_than, force=force))
    return verdicts


def _checkpoint_only_handoff(
    checkpoint: Mapping[str, Any] | None, status: str | None, active_handoff: bool
) -> bool:
    """Keep only checkpoint-only handoffs blocking; canonical pauses expire."""
    if not checkpoint or not checkpoint.get("phase_handoff_pending"):
        return False
    return not (status == PAUSE_STATUS or (status == INTERRUPTED_STATUS and active_handoff))


def _has_unidentified_nested_checkout(
    run_dir: Path, entries: list[WorkspaceCleanupVerdict]
) -> bool:
    """Fail closed if a root contains a checkout outside its known safe facts."""
    known = {
        _canonical(entry.snapshot.worktree_path)
        for entry in entries
        if entry.snapshot.worktree_path is not None
        and (entry.selected or entry.reason in _INERT_REASONS)
    }
    legacy_container = run_dir / "worktrees"
    try:
        children = tuple(legacy_container.iterdir()) if legacy_container.is_dir() else ()
        if children and not all(_contains_known_checkout(child, known) for child in children):
            return True
        for directory, dirnames, filenames in os.walk(run_dir, followlinks=False):
            if ".git" not in dirnames and ".git" not in filenames:
                continue
            checkout = _canonical(Path(directory))
            if not any(_is_within(checkout, path) for path in known):
                return True
    except OSError:
        return True
    return False


def _contains_known_checkout(path: Path, known: set[Path]) -> bool:
    candidate = _canonical(path)
    return any(_is_within(checkout, candidate) for checkout in known)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_run_root(run_dir: Path, runs_dir: Path) -> bool:
    """A root must be an ordinary direct child, never a symlink or traversal."""
    try:
        return (
            not run_dir.is_symlink()
            and run_dir.parent.resolve(strict=True) == runs_dir.resolve(strict=True)
            and run_dir.resolve(strict=True).parent == runs_dir.resolve(strict=True)
        )
    except (OSError, RuntimeError):
        return False


def _cross_parent_protection(
    snapshot: WorkspaceCleanupSnapshot,
    now: datetime,
    older_than: timedelta,
    force: bool,
) -> tuple[str, str] | None:
    """Return the fail-closed protection inherited by every cross child."""
    meta = snapshot.meta
    assert meta is not None
    try:
        status = merged_status(dict(meta), snapshot.run_dir)
    except Exception:
        return "parent_status_unknown", "cross parent lifecycle status could not be determined"
    if not isinstance(status, str) or status not in STOPPED_RETENTION_STATUSES | {PAUSE_STATUS}:
        return "parent_not_stopped", "cross parent is live or has an unknown lifecycle status"
    if force and status == PAUSE_STATUS:
        return "parent_not_stopped", "paused cross parent remains a structural protection"
    worktree = meta.get("worktree")
    retention_until = (
        worktree.get("retention_until", RETENTION_UNSET)
        if isinstance(worktree, Mapping)
        else RETENTION_UNSET
    )
    deadline, error = resolve_retention_deadline(
        snapshot.root_run_id, retention_until, older_than=older_than
    )
    if error:
        return "parent_retention_invalid", "cross parent retention deadline is malformed or unavailable"
    assert deadline is not None
    if (_active_handoff(meta) or _active_cross_gate(snapshot.run_dir, meta)) and deadline > now:
        return "parent_active_handoff_or_gate", "cross parent has an active handoff or gate"
    return None


def _reverify_group(
    verdicts: list[WorkspaceCleanupVerdict],
    runs_dir: Path,
    now: datetime,
    older_than: timedelta = timedelta(days=30),
    force: bool = False,
) -> list[WorkspaceCleanupVerdict] | None:
    """Re-check one planned physical checkout immediately before mutating it.

    State can change between selection and mutation: a run can resume, an
    agent can leave new work in the checkout.  Re-read this group's own
    durable references and re-apply the same predicate — including the git
    at-risk probe — without rescanning the whole workspace.
    """
    planned_paths = {
        _canonical(verdict.snapshot.worktree_path)
        for verdict in verdicts
        if verdict.snapshot.worktree_path is not None
    }
    if len(planned_paths) != 1:
        return None
    refreshed = [_refresh_snapshot(verdict.snapshot) for verdict in verdicts]
    fresh_paths = {
        _canonical(snapshot.worktree_path)
        for snapshot in refreshed
        if snapshot.worktree_path is not None
    }
    if fresh_paths != planned_paths:
        return None  # a reference now points elsewhere; leave this checkout alone
    for root_id in {snapshot.root_run_id for snapshot in refreshed}:
        is_cross_child = any(
            snapshot.root_run_id == root_id and snapshot.run_id != root_id for snapshot in refreshed
        )
        if is_cross_child:
            root_dir = runs_dir / root_id
            root_meta = _read_object(root_dir / "meta.json")
            if root_meta is None or not _declared_aliases(root_meta):
                return None
            root_snapshot = _snapshot(root_id, root_dir, root_meta, root_id)
            if _cross_parent_protection(root_snapshot, now, older_than, force) is not None:
                return None
    fresh = _evaluate_snapshots(refreshed, runs_dir, now, older_than, force)
    if any(not verdict.selected for verdict in fresh):
        return None
    return fresh


def _reverify_root(
    planned: RunRootVerdict,
    runs_dir: Path,
    now: datetime,
    older_than: timedelta,
    force: bool = False,
) -> RunRootVerdict | None:
    """Re-read one root and its declared children without rescanning the workspace."""
    root_id = planned.facts.root_id
    snapshots = _discover_root(runs_dir / root_id, root_id)
    individual = _evaluate_snapshots(snapshots, runs_dir, now, older_than, force)
    roots = _evaluate_roots(runs_dir, snapshots, individual, now, older_than, force)
    return next((verdict for verdict in roots if verdict.selected and verdict.facts.root_id == root_id), None)


def _refresh_snapshot(snapshot: WorkspaceCleanupSnapshot) -> WorkspaceCleanupSnapshot:
    """Re-read a snapshot from its durable location without scanning siblings."""
    durable_meta = _read_object(snapshot.meta_path)
    meta: Mapping[str, Any] | None = durable_meta
    if snapshot.meta_path != snapshot.run_dir / "meta.json":
        meta = _embedded_child(durable_meta, snapshot.run_id) if durable_meta is not None else None
    return _snapshot(
        snapshot.run_id,
        snapshot.run_dir,
        meta,
        snapshot.root_run_id,
        meta_path=snapshot.meta_path,
    )


def _snapshot(
    run_id: str,
    run_dir: Path,
    meta: Mapping[str, Any] | None,
    root_id: str,
    *,
    meta_path: Path | None = None,
) -> WorkspaceCleanupSnapshot:
    worktree = meta.get("worktree") if isinstance(meta, Mapping) else None
    path = worktree.get("path") if isinstance(worktree, Mapping) else None
    source = worktree.get("source_repo_path") if isinstance(worktree, Mapping) else None
    return WorkspaceCleanupSnapshot(
        run_id,
        run_dir,
        meta_path or run_dir / "meta.json",
        Path(path) if isinstance(path, str) and path else None,
        meta,
        root_id,
        Path(source) if isinstance(source, str) and source else None,
    )


def _individual_verdict(
    snapshot: WorkspaceCleanupSnapshot,
    runs_dir: Path,
    now: datetime,
    older_than: timedelta,
) -> WorkspaceCleanupVerdict:
    meta = snapshot.meta
    if meta is None:
        # An absent meta records nothing to reclaim; a present-but-unreadable
        # one may hide a checkout record, so only the latter fails closed.
        if not snapshot.meta_path.exists():
            return _protected(snapshot, "meta_missing", "run directory has no meta.json")
        return _protected(snapshot, "meta_unreadable", "meta.json is unreadable or malformed")
    worktree = meta.get("worktree")
    if not isinstance(worktree, Mapping):
        return _protected(
            snapshot, "worktree_missing", "run has no readable retained worktree record"
        )
    if is_worktree_reclaimed(worktree):
        return _protected(
            snapshot, "already_reclaimed", "checkout was reclaimed; recorded path is historical"
        )
    if snapshot.worktree_path is None:
        return _protected(snapshot, "worktree_incomplete", "worktree record has no checkout path")
    if not snapshot.worktree_path.exists():
        return _protected(snapshot, "worktree_gone", "recorded checkout no longer exists on disk")
    if snapshot.source_repo_path is None:
        return _protected(
            snapshot, "worktree_incomplete", "worktree record has no source repository path"
        )
    if not _safe_checkout_path(snapshot.worktree_path, runs_dir):
        return _protected(
            snapshot,
            "worktree_path_unsafe",
            "checkout escapes the resolved runspace or traverses a symlink",
        )
    try:
        status = merged_status(dict(meta), snapshot.run_dir)
    except Exception:
        return _protected(
            snapshot, "status_unknown", "merged lifecycle status could not be determined"
        )
    if not isinstance(status, str) or status not in STOPPED_RETENTION_STATUSES | {PAUSE_STATUS}:
        return _protected(
            snapshot, "status_not_stopped", "run is live or has an unknown lifecycle status"
        )
    deadline, error = resolve_retention_deadline(
        snapshot.root_run_id,
        worktree.get("retention_until", RETENTION_UNSET),
        older_than=older_than,
    )
    if error:
        return _protected(
            snapshot,
            "retention_invalid",
            "retention deadline is malformed or unavailable",
        )
    assert deadline is not None
    handoff = _active_handoff(meta)
    cross_gate = _active_cross_gate(snapshot.run_dir, meta)
    # A real cross gate is a live coordination point — fail-closed regardless of
    # age. A stale open handoff protects only within the retention window, so a
    # dead pause past its deadline no longer holds its checkout forever.
    if cross_gate or (handoff and deadline > now):
        return _protected(
            snapshot, "active_handoff_or_gate", "an operator handoff or cross gate remains active"
        )
    if deadline > now:
        return _protected(snapshot, "retention_active", "retention deadline has not expired")
    at_risk = _work_at_risk(snapshot.worktree_path)
    if at_risk is not None:
        return _protected(snapshot, *at_risk)
    reason = "pause_retention_expired" if status == PAUSE_STATUS and handoff else "retention_expired"
    return WorkspaceCleanupVerdict(snapshot, True, reason, "stopped run has an expired retained checkout")


def _work_at_risk(path: Path) -> tuple[str, str] | None:
    """Return why this checkout holds unrecoverable work, or ``None``.

    Protection answers exactly one question: *is there anything here that
    cannot be recovered from somewhere else?* Only three things qualify — an
    uncommitted change, a commit no remote has, and (checked by the caller) a
    run that may still resume in place. Registration, manifests and other
    bookkeeping prove *identity*, not value: they decide how a checkout is
    removed, never whether it may be.

    A checkout whose repository is gone (a torn test fixture, a pruned parent)
    can hold nothing recoverable — git has nowhere to recover it into — so it
    is reclaimable. A repository that exists but cannot answer is protected:
    an unanswered question is not a clean answer.
    """
    if not (path / ".git").exists():
        return None
    status = _git(path, "status", "--porcelain")
    if status is None:
        return (
            None
            if _repo_is_gone(path)
            else ("git_unreadable", "checkout has a repository that could not be read")
        )
    if status.strip():
        return "uncommitted_changes", "checkout holds changes that were never committed"
    unpushed = _git(path, "log", "--oneline", "HEAD", "--not", "--remotes")
    if unpushed is None:
        return "git_unreadable", "commits could not be compared against the remotes"
    if unpushed.strip():
        return "unpushed_commits", "checkout holds commits that no remote has"
    return None


def _git(path: Path, *args: str) -> str | None:
    """Run one read-only git command in ``path``; ``None`` when git cannot answer."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _repo_is_gone(path: Path) -> bool:
    """True when the checkout's ``.git`` points at a repository that no longer exists."""
    pointer = path / ".git"
    if pointer.is_dir():
        return False
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not raw.startswith("gitdir:"):
        return True
    return not Path(raw.split(":", 1)[1].strip()).exists()


def _registered(project_dir: Path, path: Path) -> bool:
    from pipeline.engine.worktree import registered_worktree_exists

    return registered_worktree_exists(project_dir=project_dir, path=path)


def _active_handoff(meta: Mapping[str, Any]) -> bool:
    active = meta.get("phase_handoff")
    return isinstance(active, Mapping) and bool(active.get("id"))


def _active_cross_gate(run_dir: Path, meta: Mapping[str, Any]) -> bool:
    pending = meta.get("pending_gate")
    if isinstance(pending, Mapping) and pending:
        return True
    checkpoint = _read_object(run_dir / "cross_checkpoint.json")
    return bool(
        checkpoint and (checkpoint.get("phase_handoff_pending") or checkpoint.get("pending_gate"))
    )


def _safe_checkout_path(path: Path, runs_dir: Path) -> bool:
    anchor = _checkout_anchor(path, runs_dir)
    if anchor is None:
        return False
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(anchor.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    # Do not permit a lexical component below the anchor to be a symlink.
    current = anchor
    try:
        for part in path.relative_to(anchor).parts:
            current /= part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _checkout_anchor(path: Path, runs_dir: Path) -> Path | None:
    """The sanctioned worktree container this checkout lives in, if any.

    Retained checkouts live under ``<runspace>/worktrees/``; older runs kept
    them inside their own run directory at ``<runs>/<run>/worktrees/``.  Any
    path outside both containers is not a checkout cleanup may touch.
    """
    modern = runs_dir.parent / "worktrees"
    try:
        path.relative_to(modern)
    except ValueError:
        pass
    else:
        return modern
    try:
        parts = path.relative_to(runs_dir).parts
    except ValueError:
        return None
    if len(parts) >= 3 and parts[1] == "worktrees":
        return runs_dir / parts[0] / "worktrees"
    return None


def _declared_aliases(meta: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for projects in (
        meta.get("projects"),
        meta.get("phases", {}).get("projects") if isinstance(meta.get("phases"), Mapping) else None,
    ):
        if isinstance(projects, Mapping):
            names.extend(
                key for key in projects if isinstance(key, str) and key and Path(key).name == key
            )
    return tuple(dict.fromkeys(names))


def _embedded_child(meta: Mapping[str, Any], alias: str) -> Mapping[str, Any] | None:
    phases = meta.get("phases")
    projects = phases.get("projects") if isinstance(phases, Mapping) else None
    value = projects.get(alias) if isinstance(projects, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_older_than(older_than: timedelta | None, *, force: bool) -> timedelta:
    """Keep the historic default while requiring an explicit force cutoff."""
    if force and older_than is None:
        raise ValueError("force cleanup requires an explicit older_than cutoff")
    return timedelta(days=30) if older_than is None else older_than


def _force_verdict(
    verdict: WorkspaceCleanupVerdict,
    now: datetime,
    older_than: timedelta,
    force: bool,
) -> WorkspaceCleanupVerdict:
    if verdict.snapshot.meta is None:
        # Unknown state is never force-reclaimed (symmetric with the
        # run-root rule; unreadable meta must stay exactly as protected).
        return verdict
    forced_reason = forced_reclaim_reason(
        verdict.snapshot.root_run_id,
        verdict.reason,
        now=now,
        older_than=older_than,
        force=force,
    )
    if forced_reason is None:
        return verdict
    return WorkspaceCleanupVerdict(
        verdict.snapshot, True, forced_reason, f"force reclaimed: {verdict.detail}"
    )


def _canonical(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _protected(
    snapshot: WorkspaceCleanupSnapshot, reason: str, detail: str
) -> WorkspaceCleanupVerdict:
    return WorkspaceCleanupVerdict(snapshot, False, reason, detail)


def _selected_groups(
    selected: tuple[WorkspaceCleanupVerdict, ...],
) -> dict[Path, list[WorkspaceCleanupVerdict]]:
    groups: dict[Path, list[WorkspaceCleanupVerdict]] = {}
    for verdict in selected:
        path = verdict.snapshot.worktree_path
        if path is not None:
            groups.setdefault(_canonical(path), []).append(verdict)
    return groups


def _reclaim_group(
    path: Path,
    verdicts: list[WorkspaceCleanupVerdict],
    runs_dir: Path,
    disposition: CleanupDisposition,
    receipt_path: Path,
    receipt: dict[str, Any],
    archive_root: Path | None,
    checker: RegistrationChecker,
) -> dict[str, Any]:
    """Archive (when requested), then remove by whichever route git allows."""
    source = verdicts[0].snapshot.source_repo_path
    assert source is not None  # guaranteed by the selection predicate
    size = _tree_bytes(path)
    archive_path: Path | None = None
    if disposition == "archive":
        archive_path = (
            archive_root / receipt_path.stem / "worktrees" / path.parent.name
            if archive_root is not None
            else runs_dir.parent
            / "cleanup_archive"
            / receipt_path.stem
            / "worktrees"
            / path.parent.name
        )
        try:
            _archive_snapshot(path, archive_path)
        except OSError as exc:
            return _result(path, False, size, disposition, archive_path, f"archive failed: {exc}")
        _operation(receipt, receipt_path, "archive_snapshot", path, archive_path, True, None)
    # Registration decides *how* a checkout is removed, never whether it may
    # be. A registered checkout must go through git so no stale registration
    # is left behind; an unregistered directory has no registration to damage,
    # so a plain removal is both correct and the only thing that can work.
    from pipeline.engine.worktree import reclaim_registered_worktree

    try:
        registered = checker(source, path)
    except Exception:
        registered = False
    if registered:
        removal = reclaim_registered_worktree(project_dir=source, path=path)
        operation = "registered_worktree_remove"
    else:
        removal = _remove_unregistered_checkout(path)
        operation = "unregistered_checkout_remove"
    _operation(
        receipt,
        receipt_path,
        operation,
        path,
        None,
        removal.ok,
        removal.error,
    )
    if not removal.ok:
        return _result(
            path,
            False,
            size,
            disposition,
            archive_path,
            removal.error or "git worktree removal failed",
        )
    marker = {
        "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "disposition": disposition,
        "archive_path": str(archive_path) if archive_path else None,
        "receipt_path": str(receipt_path),
    }
    try:
        _mark_reclaimed(verdicts, path, marker)
    except OSError as exc:
        # The checkout is already safely deregistered.  Leave an explicit
        # partial receipt rather than attempting a dependent root operation.
        return _result(
            path, False, size, disposition, archive_path, f"reclaimed marker failed: {exc}"
        )
    return _result(path, True, size, disposition, archive_path, None)


def _remove_unregistered_checkout(path: Path):  # noqa: ANN201 — mirrors GitOpResult
    """Remove a checkout git does not track, reporting the same result shape."""
    import shutil

    from pipeline.engine.worktree import GitOpResult

    try:
        shutil.rmtree(path)
    except OSError as exc:
        return GitOpResult(ok=False, error=f"checkout removal failed: {exc}", path=path)
    return GitOpResult(ok=True, error=None, path=path)


def _reclaim_run_root(
    run_dir: Path,
    runspace: Path,
    disposition: CleanupDisposition,
    receipt_path: Path,
    receipt: dict[str, Any],
    archive_root: Path | None,
) -> dict[str, Any]:
    """Archive/delete a root only after its worktree groups have succeeded."""
    if not run_dir.is_dir():
        return _result(run_dir, False, 0, disposition, None, "run root is missing")
    size = _tree_bytes(run_dir)
    archive_path: Path | None = None
    if disposition == "archive":
        archive_path = (
            archive_root / receipt_path.stem / "runs" / run_dir.name
            if archive_root is not None
            else runspace / "cleanup_archive" / receipt_path.stem / "runs" / run_dir.name
        )
        try:
            _archive_snapshot(run_dir, archive_path)
        except OSError as exc:
            return _result(
                run_dir, False, size, disposition, archive_path, f"run archive failed: {exc}"
            )
        _operation(receipt, receipt_path, "run_archive_snapshot", run_dir, archive_path, True, None)
    # This is deliberately a run-root operation, never a worktree-path
    # operation.  Registered checkouts above only go through worktree.py.
    try:
        shutil.rmtree(run_dir)
    except OSError as exc:
        _operation(receipt, receipt_path, "run_root_remove", run_dir, None, False, str(exc))
        return _result(
            run_dir, False, size, disposition, archive_path, f"run root removal failed: {exc}"
        )
    _operation(receipt, receipt_path, "run_root_remove", run_dir, None, True, None)
    return _result(run_dir, True, size, disposition, archive_path, None)


def _archive_snapshot(source: Path, destination: Path) -> None:
    """Create and verify a lossless, non-active filesystem snapshot."""
    if destination.exists():
        raise OSError(f"archive destination already exists: {destination}")
    shutil.copytree(source, destination, symlinks=True)
    if _tree_digest(source) != _tree_digest(destination):
        # This destination is not a registered checkout and is safe to discard.
        shutil.rmtree(destination)
        raise OSError("archive verification digest mismatch")


def _mark_reclaimed(
    verdicts: list[WorkspaceCleanupVerdict],
    path: Path,
    marker: Mapping[str, Any],
) -> None:
    """Atomically stamp every retained meta file that references ``path``."""
    for meta_path in sorted({v.snapshot.meta_path for v in verdicts}):
        meta = _read_object(meta_path)
        if meta is None:
            raise OSError(f"cannot re-read metadata for reclaimed marker: {meta_path}")
        if not _stamp_reclaimed(meta, path, marker):
            raise OSError(f"metadata no longer references reclaimed checkout: {meta_path}")
        _write_atomic(meta_path, meta)


def _stamp_reclaimed(value: Any, path: Path, marker: Mapping[str, Any]) -> bool:
    found = False
    if isinstance(value, dict):
        worktree = value.get("worktree")
        if (
            isinstance(worktree, dict)
            and isinstance(worktree.get("path"), str)
            and _canonical(Path(worktree["path"])) == _canonical(path)
        ):
            worktree["reclaimed"] = dict(marker)
            found = True
        for child in value.values():
            found = _stamp_reclaimed(child, path, marker) or found
    elif isinstance(value, list):
        for child in value:
            found = _stamp_reclaimed(child, path, marker) or found
    return found


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _tree_bytes(path: Path) -> int:
    total = 0
    for item in _tree_items(path):
        if item.is_file() and not item.is_symlink():
            with suppress(OSError):
                total += item.stat().st_size
    return total


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in _tree_items(path):
        relative = item.relative_to(path).as_posix().encode()
        if item.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + str(item.readlink()).encode())
        elif item.is_dir():
            digest.update(b"D\0" + relative)
        elif item.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with item.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def _tree_items(path: Path) -> list[Path]:
    try:
        return sorted(path.rglob("*"), key=lambda item: item.as_posix())
    except OSError as exc:
        raise OSError(f"cannot scan {path}: {exc}") from exc


def _verdict_payload(verdict: WorkspaceCleanupVerdict) -> dict[str, Any]:
    return {
        "run_id": verdict.snapshot.run_id,
        "root_run_id": verdict.snapshot.root_run_id,
        "path": str(verdict.snapshot.worktree_path) if verdict.snapshot.worktree_path else None,
        "reason": verdict.reason,
        "detail": verdict.detail,
    }


def _root_verdict_payload(verdict: RunRootVerdict) -> dict[str, Any]:
    return {
        "root_run_id": verdict.facts.root_id,
        "path": str(verdict.facts.run_dir),
        "reason": verdict.reason,
        "detail": verdict.detail,
        "dependency_paths": [str(path) for path in verdict.facts.dependency_paths],
    }


def _selected_bytes(selected: tuple[WorkspaceCleanupVerdict, ...]) -> int:
    return sum(_tree_bytes(path) for path in _selected_groups(selected))


def _result(
    path: Path,
    ok: bool,
    size: int,
    disposition: CleanupDisposition,
    archive_path: Path | None,
    error: str | None,
) -> dict[str, Any]:
    return {
        "kind": "worktree" if path.name == "checkout" else "run_root",
        "path": str(path),
        "ok": ok,
        "disposition": disposition,
        "archive_path": str(archive_path) if archive_path else None,
        "bytes": size,
        "error": error,
    }


def _operation(
    receipt: dict[str, Any],
    receipt_path: Path,
    kind: str,
    path: Path,
    archive_path: Path | None,
    ok: bool,
    error: str | None,
) -> None:
    receipt["operations"].append(
        {
            "kind": kind,
            "path": str(path),
            "archive_path": str(archive_path) if archive_path else None,
            "ok": ok,
            "error": error,
        }
    )
    _write_atomic(receipt_path, receipt)
