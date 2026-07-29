"""Fail-closed discovery and selection for retained workspace worktrees.

This module does not archive or delete anything.  Both the report surface and
the future mutation surface consume :func:`select_workspace_cleanup`, which
makes it impossible for the two to drift in their safety predicate.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pipeline.engine.worktree import is_worktree_reclaimed
from pipeline.run_state.setup_failure import merged_status
from pipeline.run_state.status_vocab import STOPPED_RETENTION_STATUSES

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
    """Immutable, read-only selection output shared by report and execution."""

    runs_dir: Path
    selected: tuple[WorkspaceCleanupVerdict, ...]
    protected: tuple[WorkspaceCleanupVerdict, ...]
    root_run_ids_for_both: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupExecution:
    """Receipt location and final durable payload from one cleanup attempt."""

    receipt_path: Path
    receipt: Mapping[str, Any]


def select_workspace_cleanup(
    runs_dir: Path | str,
    *,
    now: datetime | None = None,
    registration_checker: RegistrationChecker | None = None,
) -> WorkspaceCleanupPlan:
    """Discover retained worktrees and return one fail-closed selection plan.

    ``runs_dir`` is the already-resolved runs-dir contract (not a workspace
    search).  Every malformed, incomplete, external, or live observation is a
    protected verdict.  A physical checkout is selected only once all of its
    references are individually eligible.
    """
    root = Path(runs_dir)
    instant = _utc_now(now)
    checker = registration_checker or _registered
    snapshots = _discover(root)
    individual = _evaluate_snapshots(snapshots, root, instant, checker)

    # A cross root is actionable only after every declared child checkout was
    # selected.  This does not make a root itself a deletion instruction; it is
    # merely the guarded eligibility signal consumed by the later both tier.
    by_root: dict[str, list[WorkspaceCleanupVerdict]] = {}
    for verdict in individual:
        # A cross parent normally owns no checkout itself.  Its ``both``
        # eligibility is derived from the explicitly declared child records,
        # while a mono root remains its own physical-worktree record.
        if (
            verdict.snapshot.run_id == verdict.snapshot.root_run_id
            and verdict.snapshot.meta is not None
            and _declared_aliases(verdict.snapshot.meta)
        ):
            continue
        by_root.setdefault(verdict.snapshot.root_run_id, []).append(verdict)
    both = tuple(sorted(
        root_id for root_id, verdicts in by_root.items()
        if verdicts and all(v.selected for v in verdicts)
    ))
    selected = tuple(v for v in individual if v.selected)
    protected = tuple(v for v in individual if not v.selected)
    return WorkspaceCleanupPlan(root, selected, protected, both)


def execute_workspace_cleanup(
    runs_dir: Path | str,
    *,
    tier: CleanupTier = "worktrees",
    disposition: CleanupDisposition = "archive",
    now: datetime | None = None,
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
    root = Path(runs_dir)
    instant = _utc_now(now)
    checker = registration_checker or _registered
    plan = select_workspace_cleanup(root, now=instant, registration_checker=checker)
    receipt_path = root.parent / "cleanup_receipts" / f"cleanup-{uuid.uuid4().hex}.json"
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": receipt_path.stem,
        "created_at": instant.isoformat().replace("+00:00", "Z"),
        "runs_dir": str(root),
        "tier": tier,
        "disposition": disposition,
        "status": "running",
        "selected": [_verdict_payload(v) for v in plan.selected],
        "protected": [_verdict_payload(v) for v in plan.protected],
        "results": [],
        "operations": [],
        "errors": [],
        "bytes_selected": _selected_bytes(plan.selected),
        "bytes_archived": 0,
        "bytes_reclaimed": 0,
    }
    _write_atomic(receipt_path, receipt)  # durable boundary before mutation

    groups = _selected_groups(plan.selected)
    succeeded_roots: set[str] = set()
    failed_roots: set[str] = set()
    for physical, verdicts in groups.items():
        # State can change after reporting. Re-evaluate this physical group's
        # durable references immediately before mutation.  This applies the
        # same individual/shared/parent predicate without repeatedly scanning
        # every run or invoking git for unrelated checkouts.
        fresh_verdicts = _reverify_group(verdicts, root, instant, checker)
        if fresh_verdicts is None:
            receipt["protected"].append({
                "path": str(physical), "reason": "changed_before_execution",
                "detail": "selection predicate no longer permits reclamation",
            })
            failed_roots.update(v.snapshot.root_run_id for v in verdicts)
            _write_atomic(receipt_path, receipt)
            continue
        result = _reclaim_group(
            physical, fresh_verdicts, root, disposition, receipt_path, receipt, archive_root,
        )
        receipt["results"].append(result)
        if result["ok"]:
            succeeded_roots.update(v.snapshot.root_run_id for v in verdicts)
            if disposition == "archive":
                receipt["bytes_archived"] += result["bytes"]
            else:
                receipt["bytes_reclaimed"] += result["bytes"]
        else:
            failed_roots.update(v.snapshot.root_run_id for v in verdicts)
            receipt["errors"].append(result["error"])
        _write_atomic(receipt_path, receipt)

    if tier == "both":
        for root_id in plan.root_run_ids_for_both:
            if root_id in failed_roots or root_id not in succeeded_roots:
                continue
            root_result = _reclaim_run_root(
                root / root_id, root.parent, disposition, receipt_path, receipt, archive_root,
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
        return [WorkspaceCleanupSnapshot(
            "<runs-dir>", runs_dir, runs_dir / "meta.json", None, None,
            "<runs-dir>", None,
        )]
    snapshots: list[WorkspaceCleanupSnapshot] = []
    for run_dir in roots:
        meta = _read_object(run_dir / "meta.json")
        root_id = run_dir.name
        snapshots.append(_snapshot(root_id, run_dir, meta, root_id))
        if meta is None:
            continue
        for alias in _declared_aliases(meta):
            child_dir = run_dir / alias
            # Direct child ownership forbids aliases such as ../other-run.
            if child_dir.parent != run_dir or not child_dir.is_dir():
                snapshots.append(WorkspaceCleanupSnapshot(
                    alias, child_dir, child_dir / "meta.json", None, None,
                    root_id, None,
                ))
                embedded = _embedded_child(meta, alias)
                if embedded is not None:
                    snapshots.append(_snapshot(
                        alias, child_dir, embedded, root_id, meta_path=run_dir / "meta.json",
                    ))
                continue
            child_meta = _read_object(child_dir / "meta.json")
            snapshots.append(_snapshot(alias, child_dir, child_meta, root_id))
            embedded = _embedded_child(meta, alias)
            if embedded is not None:
                # The embedded session is a separate durable reference.  It
                # cannot overwrite the child file: disagreement protects the
                # physical checkout through the normal shared-path grouping.
                snapshots.append(_snapshot(
                    alias, child_dir, embedded, root_id, meta_path=run_dir / "meta.json",
                ))
    return snapshots


def _evaluate_snapshots(
    snapshots: list[WorkspaceCleanupSnapshot],
    runs_dir: Path,
    now: datetime,
    checker: RegistrationChecker,
) -> list[WorkspaceCleanupVerdict]:
    """Apply the shared individual, parent, and shared-checkout predicate."""
    individual = [_individual_verdict(snapshot, runs_dir, now, checker) for snapshot in snapshots]
    parent_guards = {
        snapshot.root_run_id: _cross_parent_protection(snapshot)
        for snapshot in snapshots
        if snapshot.run_id == snapshot.root_run_id
        and snapshot.meta is not None
        and _declared_aliases(snapshot.meta)
    }
    for index, verdict in enumerate(individual):
        parent_guard = parent_guards.get(verdict.snapshot.root_run_id)
        if parent_guard is not None and verdict.snapshot.run_id != verdict.snapshot.root_run_id:
            individual[index] = _protected(verdict.snapshot, *parent_guard)

    by_checkout: dict[Path, list[int]] = {}
    for index, verdict in enumerate(individual):
        path = verdict.snapshot.worktree_path
        if path is not None:
            by_checkout.setdefault(_canonical(path), []).append(index)
    for indexes in by_checkout.values():
        if not _shared_references_proven(indexes, individual):
            for index in indexes:
                old = individual[index]
                individual[index] = WorkspaceCleanupVerdict(
                    old.snapshot, False, "shared_reference_unproven",
                    "manifest attachment cannot be proven to have a readable matching run record",
                )
            continue
        if any(not individual[index].selected for index in indexes):
            for index in indexes:
                if individual[index].selected:
                    old = individual[index]
                    individual[index] = WorkspaceCleanupVerdict(
                        old.snapshot, False, "shared_checkout_protected",
                        "another run still protects this shared physical checkout",
                    )
    return individual


def _cross_parent_protection(
    snapshot: WorkspaceCleanupSnapshot,
) -> tuple[str, str] | None:
    """Return the fail-closed protection inherited by every cross child."""
    meta = snapshot.meta
    assert meta is not None
    if _active_handoff(meta) or _active_cross_gate(snapshot.run_dir, meta):
        return "parent_active_handoff_or_gate", "cross parent has an active handoff or gate"
    try:
        status = merged_status(dict(meta), snapshot.run_dir)
    except Exception:
        return "parent_status_unknown", "cross parent lifecycle status could not be determined"
    if not isinstance(status, str) or status not in STOPPED_RETENTION_STATUSES:
        return "parent_not_stopped", "cross parent is live or has an unknown lifecycle status"
    return None


def _reverify_group(
    verdicts: list[WorkspaceCleanupVerdict],
    runs_dir: Path,
    now: datetime,
    checker: RegistrationChecker,
) -> list[WorkspaceCleanupVerdict] | None:
    """Refresh only one planned physical checkout before mutating it."""
    # Re-discover here instead of refreshing only known meta files.  A
    # follow-up can attach to this checkout after selection; its run id is
    # recorded in the manifest and must fail closed until its matching meta is
    # readable and eligible too.
    planned_paths = {
        _canonical(verdict.snapshot.worktree_path)
        for verdict in verdicts
        if verdict.snapshot.worktree_path is not None
    }
    discovered = _discover(runs_dir)
    refreshed = [
        snapshot for snapshot in discovered
        if snapshot.worktree_path is not None
        and _canonical(snapshot.worktree_path) in planned_paths
    ]
    if not refreshed:
        return None
    for root_id in {snapshot.root_run_id for snapshot in refreshed}:
        root_dir = runs_dir / root_id
        root_meta = _read_object(root_dir / "meta.json")
        is_cross_root = any(
            snapshot.root_run_id == root_id and snapshot.run_id != root_id
            for snapshot in refreshed
        )
        if is_cross_root:
            if root_meta is None or not _declared_aliases(root_meta):
                return None
            root_snapshot = _snapshot(root_id, root_dir, root_meta, root_id)
            if _cross_parent_protection(root_snapshot) is not None:
                return None
    fresh = _evaluate_snapshots(refreshed, runs_dir, now, checker)
    paths = {_canonical(verdict.snapshot.worktree_path) for verdict in fresh if verdict.snapshot.worktree_path}
    if len(paths) != 1 or any(not verdict.selected for verdict in fresh):
        return None
    return fresh


def _shared_references_proven(
    indexes: list[int],
    verdicts: list[WorkspaceCleanupVerdict],
) -> bool:
    """Require each manifest attachment to resolve to the same checkout.

    The manifest is the authoritative complete list for a shared checkout.
    Discovery can observe a torn or unreadable attached run, in which case it
    must protect the checkout rather than allowing a dangling historical path.
    """
    snapshots = [verdict.snapshot for verdict in verdicts]
    for index in indexes:
        snapshot = verdicts[index].snapshot
        worktree = snapshot.meta.get("worktree") if snapshot.meta is not None else None
        if not isinstance(worktree, Mapping) or snapshot.worktree_path is None:
            return False
        manifest = _manifest_for_checkout(snapshot.worktree_path, worktree)
        if manifest is None:
            return False
        attached = manifest.get("attached_run_ids")
        if not isinstance(attached, list) or not attached or any(
            not isinstance(run_id, str) or not run_id.strip() for run_id in attached
        ):
            return False
        checkout = _canonical(snapshot.worktree_path)
        for run_id in attached:
            if not any(
                candidate.run_id == run_id
                and candidate.meta is not None
                and candidate.worktree_path is not None
                and _canonical(candidate.worktree_path) == checkout
                for candidate in snapshots
            ):
                return False
    return True


def _refresh_snapshot(snapshot: WorkspaceCleanupSnapshot) -> WorkspaceCleanupSnapshot:
    """Re-read a snapshot from its durable location without scanning siblings."""
    durable_meta = _read_object(snapshot.meta_path)
    meta: Mapping[str, Any] | None = durable_meta
    if snapshot.meta_path != snapshot.run_dir / "meta.json":
        meta = _embedded_child(durable_meta, snapshot.run_id) if durable_meta is not None else None
    return _snapshot(
        snapshot.run_id, snapshot.run_dir, meta, snapshot.root_run_id,
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
        run_id, run_dir, meta_path or run_dir / "meta.json",
        Path(path) if isinstance(path, str) and path else None,
        meta, root_id, Path(source) if isinstance(source, str) and source else None,
    )


def _individual_verdict(snapshot: WorkspaceCleanupSnapshot, runs_dir: Path, now: datetime, checker: RegistrationChecker) -> WorkspaceCleanupVerdict:
    meta = snapshot.meta
    if meta is None:
        return _protected(snapshot, "meta_unreadable", "meta.json is missing, unreadable, or malformed")
    worktree = meta.get("worktree")
    if not isinstance(worktree, Mapping):
        return _protected(snapshot, "worktree_missing", "run has no readable retained worktree record")
    if is_worktree_reclaimed(worktree):
        return _protected(snapshot, "already_reclaimed", "checkout was reclaimed; recorded path is historical")
    if snapshot.worktree_path is None or snapshot.source_repo_path is None:
        return _protected(snapshot, "worktree_incomplete", "worktree path or source repository proof is absent")
    if _active_handoff(meta) or _active_cross_gate(snapshot.run_dir, meta):
        return _protected(snapshot, "active_handoff_or_gate", "an operator handoff or cross gate remains active")
    try:
        status = merged_status(dict(meta), snapshot.run_dir)
    except Exception:
        return _protected(snapshot, "status_unknown", "merged lifecycle status could not be determined")
    if not isinstance(status, str) or status not in STOPPED_RETENTION_STATUSES:
        return _protected(snapshot, "status_not_stopped", "run is live or has an unknown lifecycle status")
    deadline = _parse_deadline(worktree.get("retention_until"))
    if deadline is None:
        return _protected(snapshot, "retention_invalid", "retention_until is absent or is not an ISO-8601 UTC timestamp")
    if deadline > now:
        return _protected(snapshot, "retention_active", "retention deadline has not expired")
    if not _safe_checkout_path(snapshot.worktree_path, runs_dir):
        return _protected(snapshot, "worktree_path_unsafe", "checkout escapes the resolved runspace or traverses a symlink")
    if not _manifest_proves(snapshot.worktree_path, worktree):
        return _protected(snapshot, "manifest_unproven", "worktree manifest is absent, unreadable, or disagrees with metadata")
    try:
        registered = checker(snapshot.source_repo_path, snapshot.worktree_path)
    except Exception:
        registered = False
    if not registered:
        return _protected(snapshot, "registration_unproven", "git worktree registration could not be proven")
    return WorkspaceCleanupVerdict(snapshot, True, "retention_expired", "stopped run has an expired retained checkout")


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
    return bool(checkpoint and (checkpoint.get("phase_handoff_pending") or checkpoint.get("pending_gate")))


def _parse_deadline(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC)


def _safe_checkout_path(path: Path, runs_dir: Path) -> bool:
    runspace = runs_dir.parent
    expected = runspace / "worktrees"
    try:
        path.relative_to(expected)
        resolved = path.resolve(strict=True)
        resolved.relative_to(expected.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    # Do not permit a lexical component below worktrees to be a symlink.
    current = expected
    try:
        for part in path.relative_to(expected).parts:
            current /= part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _manifest_proves(path: Path, worktree: Mapping[str, Any]) -> bool:
    return _manifest_for_checkout(path, worktree) is not None


def _manifest_for_checkout(path: Path, worktree: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = worktree.get("manifest_path")
    manifest_path = Path(raw) if isinstance(raw, str) and raw else path.parent / "manifest.json"
    try:
        manifest_path.relative_to(path.parent)
        manifest_path.resolve(strict=True).relative_to(path.parent.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    manifest = _read_object(manifest_path)
    if manifest is None:
        return None
    checkout = manifest.get("checkout_path")
    if not isinstance(checkout, str) or _canonical(Path(checkout)) != _canonical(path):
        return None
    return manifest


def _declared_aliases(meta: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for projects in (
        meta.get("projects"),
        meta.get("phases", {}).get("projects") if isinstance(meta.get("phases"), Mapping) else None,
    ):
        if isinstance(projects, Mapping):
            names.extend(key for key in projects if isinstance(key, str) and key and Path(key).name == key)
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


def _canonical(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _protected(snapshot: WorkspaceCleanupSnapshot, reason: str, detail: str) -> WorkspaceCleanupVerdict:
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
) -> dict[str, Any]:
    """Archive (when requested), then remove via the engine worktree seam."""
    source = verdicts[0].snapshot.source_repo_path
    assert source is not None  # guaranteed by the selection predicate
    size = _tree_bytes(path)
    archive_path: Path | None = None
    if disposition == "archive":
        archive_path = (
            archive_root / receipt_path.stem / "worktrees" / path.parent.name
            if archive_root is not None
            else runs_dir.parent / "cleanup_archive" / receipt_path.stem / "worktrees" / path.parent.name
        )
        try:
            _archive_snapshot(path, archive_path)
        except OSError as exc:
            return _result(path, False, size, disposition, archive_path, f"archive failed: {exc}")
        _operation(receipt, receipt_path, "archive_snapshot", path, archive_path, True, None)
    from pipeline.engine.worktree import reclaim_registered_worktree
    removal = reclaim_registered_worktree(project_dir=source, path=path)
    _operation(
        receipt, receipt_path, "registered_worktree_remove", path, None,
        removal.ok, removal.error,
    )
    if not removal.ok:
        return _result(
            path, False, size, disposition, archive_path,
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
        return _result(path, False, size, disposition, archive_path, f"reclaimed marker failed: {exc}")
    return _result(path, True, size, disposition, archive_path, None)


def _reclaim_run_root(
    run_dir: Path, runspace: Path, disposition: CleanupDisposition, receipt_path: Path,
    receipt: dict[str, Any], archive_root: Path | None,
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
            return _result(run_dir, False, size, disposition, archive_path, f"run archive failed: {exc}")
        _operation(receipt, receipt_path, "run_archive_snapshot", run_dir, archive_path, True, None)
    # This is deliberately a run-root operation, never a worktree-path
    # operation.  Registered checkouts above only go through worktree.py.
    try:
        shutil.rmtree(run_dir)
    except OSError as exc:
        _operation(receipt, receipt_path, "run_root_remove", run_dir, None, False, str(exc))
        return _result(run_dir, False, size, disposition, archive_path, f"run root removal failed: {exc}")
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
    verdicts: list[WorkspaceCleanupVerdict], path: Path, marker: Mapping[str, Any],
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


def _selected_bytes(selected: tuple[WorkspaceCleanupVerdict, ...]) -> int:
    return sum(_tree_bytes(path) for path in _selected_groups(selected))


def _result(
    path: Path, ok: bool, size: int, disposition: CleanupDisposition,
    archive_path: Path | None, error: str | None,
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
    receipt: dict[str, Any], receipt_path: Path, kind: str, path: Path,
    archive_path: Path | None, ok: bool, error: str | None,
) -> None:
    receipt["operations"].append({
        "kind": kind,
        "path": str(path),
        "archive_path": str(archive_path) if archive_path else None,
        "ok": ok,
        "error": error,
    })
    _write_atomic(receipt_path, receipt)
