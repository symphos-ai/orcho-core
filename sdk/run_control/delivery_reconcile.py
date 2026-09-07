# SPDX-License-Identifier: Apache-2.0
"""Operator reconciliation of a delivery commit the run never recorded (ADR 0191).

Two surfaces, both client-neutral (no printing, no terminal layer):

- :func:`inspect_delivery_reconciliation` — read-only. Compares the run's
  durable delivery record with what Git and the delivery ledger say, and
  returns the typed :class:`DeliveryReconciliationState` a CLI / MCP client
  renders before an operator decides anything.
- :func:`reconcile_delivery_record` — the command. An operator who verified
  that a commit in the target checkout is this run's delivery records it:
  the audit artifact is written with ``operator`` / ``note``, the durable
  ``commit_delivery`` block is set with ``provenance='reconciled'``, and the
  run's terminal status is settled through the same reducers finalization
  uses — so a rejected release that nonetheless landed reads as a reconciled
  override, never as a clean success and never as an operator approval.

Discipline: this module reads / writes durable run artifacts only. It never
mutates the checkout — the commit already exists; that is the whole point.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.engine.delivery_ledger import (
    describe_commit,
    reconcile_delivery,
    safe_decision_id,
)
from pipeline.run_state.release_verdict import is_release_blocked, normalize_verdict
from pipeline.run_state.terminal_outcome import (
    normalize_engine_reason,
    resolve_rejected_release_terminal,
    settle_delivery_terminal,
)
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

__all__ = [
    "DeliveryReconcileResult",
    "DeliveryReconciliationState",
    "inspect_delivery_reconciliation",
    "reconcile_delivery_record",
]

_DELIVERED_STATUSES: frozenset[str] = frozenset({"committed", "applied_uncommitted"})


@dataclass(frozen=True, slots=True)
class DeliveryReconciliationState:
    """Read-only comparison of the run's delivery record with Git."""

    run_id: str
    state: str
    commit_sha: str | None
    detail: str
    ledger_stage: str | None
    recorded_status: str | None
    recorded_sha: str | None
    consistent: bool
    commit_target: str | None
    commit: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "commit_sha": self.commit_sha,
            "detail": self.detail,
            "ledger_stage": self.ledger_stage,
            "recorded_status": self.recorded_status,
            "recorded_sha": self.recorded_sha,
            "consistent": self.consistent,
            "commit_target": self.commit_target,
            "commit": self.commit,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReconcileResult:
    """Outcome of :func:`reconcile_delivery_record`."""

    run_id: str
    accepted: bool
    state: str
    commit_sha: str | None = None
    blocker: str | None = None
    reason: str = ""
    artifact_path: str | None = None
    terminal_outcome: str | None = None
    release_verdict: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "accepted": self.accepted,
            "state": self.state,
            "commit_sha": self.commit_sha,
            "blocker": self.blocker,
            "reason": self.reason,
            "artifact_path": self.artifact_path,
            "terminal_outcome": self.terminal_outcome,
            "release_verdict": self.release_verdict,
            "notes": list(self.notes),
        }


def inspect_delivery_reconciliation(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    meta: dict[str, Any] | None = None,
) -> DeliveryReconciliationState:
    """Compare the run's delivery record with the ledger and Git, read-only.

    ``consistent`` is ``True`` when nothing needs recording: no delivery commit
    exists, or the durable ``commit_delivery`` block already names it.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("inspect_delivery_reconciliation: run_id must be a non-empty string")
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    resolved_meta = meta if isinstance(meta, dict) else load_meta(ref.run_dir)
    project, _worktree, _baseline = _checkout_facts(resolved_meta)
    recon = reconcile_delivery(
        ref.run_dir,
        run_id=ref.run_id,
        decision_id=safe_decision_id(ref.run_id),
        project_path=project,
    )
    recorded = resolved_meta.get("commit_delivery")
    recorded_status = (
        _optional_str(recorded.get("status")) if isinstance(recorded, dict) else None
    )
    recorded_sha = (
        _optional_str(recorded.get("commit_sha"))
        or _optional_str(recorded.get("published_commit_sha"))
        if isinstance(recorded, dict) else None
    )
    consistent = (not recon.found) or (
        recorded_status in _DELIVERED_STATUSES and recorded_sha == recon.commit_sha
    )
    target = (
        Path(recon.record.commit_target) if recon.record is not None
        else (Path(project) if project else None)
    )
    facts = (
        describe_commit(target, recon.commit_sha)
        if recon.found and recon.commit_sha and target is not None else None
    )
    return DeliveryReconciliationState(
        run_id=ref.run_id,
        state=recon.state,
        commit_sha=recon.commit_sha,
        detail=recon.detail,
        ledger_stage=recon.record.stage if recon.record is not None else None,
        recorded_status=recorded_status,
        recorded_sha=recorded_sha,
        consistent=consistent,
        commit_target=str(target) if target is not None else None,
        commit=(
            {
                "sha": facts.sha,
                "parents": list(facts.parents),
                "subject": facts.message.splitlines()[0] if facts.message else "",
                "files": len(facts.files),
                "author": facts.author,
                "committed_at": facts.committed_at,
            }
            if facts is not None else None
        ),
    )


def reconcile_delivery_record(
    run_id: str,
    *,
    operator: str,
    commit: str | None = None,
    note: str | None = None,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> DeliveryReconcileResult:
    """Record an existing delivery commit as this run's delivery.

    ``commit`` (a sha or unique prefix) must name the commit reconciliation
    found; passing it is how the operator states they verified that exact
    commit. Refusals are typed (``accepted=False`` + ``blocker``), never
    exceptions: ``no_delivery_commit_found``, ``already_recorded``,
    ``commit_mismatch``, ``commit_unreadable``.

    Raises:
        ValueError: ``run_id`` or ``operator`` empty — a programming error.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("reconcile_delivery_record: run_id must be a non-empty string")
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("reconcile_delivery_record: operator must be a non-empty string")

    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    state = inspect_delivery_reconciliation(
        ref.run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd, meta=meta,
    )
    if not state.commit_sha:
        return DeliveryReconcileResult(
            run_id=ref.run_id, accepted=False, state=state.state,
            blocker="no_delivery_commit_found",
            reason=state.detail or (
                "no delivery commit for this run was found in the target checkout"
            ),
        )
    if state.consistent:
        return DeliveryReconcileResult(
            run_id=ref.run_id, accepted=False, state=state.state,
            commit_sha=state.commit_sha, blocker="already_recorded",
            reason=(
                f"the run already records delivery commit "
                f"{state.commit_sha[:12]} as {state.recorded_status}"
            ),
        )
    wanted = (commit or "").strip().lower()
    if wanted and not state.commit_sha.lower().startswith(wanted):
        return DeliveryReconcileResult(
            run_id=ref.run_id, accepted=False, state=state.state,
            commit_sha=state.commit_sha, blocker="commit_mismatch",
            reason=(
                f"reconciliation found delivery commit {state.commit_sha[:12]}, "
                f"not {commit}; pass the commit you verified"
            ),
        )
    target = Path(state.commit_target) if state.commit_target else None
    facts = describe_commit(target, state.commit_sha) if target is not None else None
    if facts is None:
        return DeliveryReconcileResult(
            run_id=ref.run_id, accepted=False, state=state.state,
            commit_sha=state.commit_sha, blocker="commit_unreadable",
            reason=f"commit {state.commit_sha[:12]} could not be read from {target}",
        )

    from pipeline.engine.commit_delivery import persist_reconciled_delivery

    project, worktree, baseline = _checkout_facts(meta)
    notes: list[str] = []
    final_acceptance, fa_source = _final_acceptance_record(meta, ref.run_dir, ref.run_id)
    if final_acceptance is not None and fa_source == "checkpoint":
        notes.append(
            "final_acceptance restored into meta from the checkpoint store "
            "(the run stopped before meta recorded it)"
        )
    verdict = (
        normalize_verdict(final_acceptance.get("verdict")) if final_acceptance else None
    )
    blockers_raw = final_acceptance.get("release_blockers") if final_acceptance else None
    blockers = list(blockers_raw) if isinstance(blockers_raw, list) else []
    short_summary = final_acceptance.get("short_summary") if final_acceptance else None
    release_fields = {
        "release_summary": str(short_summary or ""),
        "release_verdict": verdict or "",
        "release_blockers": tuple(dict(b) for b in blockers if isinstance(b, dict)),
    }
    decision = persist_reconciled_delivery(
        ref.run_dir,
        run_id=ref.run_id,
        project_dir=target,
        source_worktree=Path(worktree) if worktree else target,
        baseline_ref=baseline,
        commit_sha=facts.sha,
        message=facts.message.strip() or f"chore: deliver orcho run {ref.run_id}",
        files_staged=facts.files,
        operator=operator.strip(),
        note=note,
        release_fields=release_fields,
    )

    meta["commit_delivery"] = decision.to_dict()
    if final_acceptance is not None and fa_source == "checkpoint":
        phases = meta.get("phases")
        if not isinstance(phases, dict):
            phases = {}
            meta["phases"] = phases
        phases["final_acceptance"] = final_acceptance
    settle_delivery_terminal(
        meta,
        applied_status="committed",
        halt_reason="commit_delivery_failed",
        halted_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    if final_acceptance is not None:
        rejected = (
            is_release_blocked(verdict, empty_blocks=False)
            or final_acceptance.get("approved") is False
        )
        resolve_rejected_release_terminal(
            meta,
            rejected=rejected,
            delivery_status="committed",
            verdict=verdict or "REJECTED",
            blockers=blockers,
            short_summary=short_summary,
            engine_reason=normalize_engine_reason(
                verification_gaps=final_acceptance.get("verification_gaps"),
                engine_backstop=final_acceptance.get("engine_backstop"),
            ),
            delivery_provenance="reconciled",
        )
    else:
        notes.append(
            "no final_acceptance record exists for this run; the release "
            "verdict is unknown and was not reconciled"
        )
    _write_meta(ref.run_dir, meta)
    return DeliveryReconcileResult(
        run_id=ref.run_id,
        accepted=True,
        state=state.state,
        commit_sha=facts.sha,
        artifact_path=str(decision.artifact_path) if decision.artifact_path else None,
        terminal_outcome=_optional_str(meta.get("status")),
        release_verdict=verdict,
        notes=tuple(notes),
    )


# ── durable-fact readers ──────────────────────────────────────────────────────


def _checkout_facts(meta: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """``(project_path, run_worktree_path, baseline_ref)`` from durable meta."""
    project = _optional_str(meta.get("project"))
    worktree = meta.get("worktree")
    worktree_path = (
        _optional_str(worktree.get("path")) if isinstance(worktree, dict) else None
    )
    baseline = (
        _optional_str(worktree.get("base_ref")) if isinstance(worktree, dict) else None
    )
    return project, worktree_path, baseline or "HEAD"


def _final_acceptance_record(
    meta: dict[str, Any], run_dir: Path, run_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """The durable final_acceptance record and where it came from.

    ``meta.phases.final_acceptance`` is authoritative when present. A run
    that stopped between the phase and the meta save (the ADR 0191 crash) has
    the record only in the checkpoint store, which is read strictly read-only.
    """
    phases = meta.get("phases")
    if isinstance(phases, dict) and isinstance(phases.get("final_acceptance"), dict):
        return dict(phases["final_acceptance"]), "meta"
    record = _final_acceptance_from_checkpoint(run_dir, run_id)
    if record is not None:
        return record, "checkpoint"
    return None, None


def _final_acceptance_from_checkpoint(run_dir: Path, run_id: str) -> dict[str, Any] | None:
    db = run_dir / "checkpoints.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT data_json FROM checkpoints WHERE run_id = ? "
                "AND phase = 'final_acceptance' ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
