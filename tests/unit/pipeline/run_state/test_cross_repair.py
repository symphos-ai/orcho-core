"""Unit tests for the conservative cross-run repair (Stage 3c).

Covers :func:`pipeline.run_state.cross_repair.repair_cross_run_state`:

- dry-run report for a terminal cross run carrying a stale handoff;
- ``apply`` clears ``meta.phase_handoff``, writes a backup + audit artifact
  under ``run_state_repairs/``, and is idempotent on a second ``apply``;
- every ambiguous cross code stays strictly diagnostic (``applied=False``,
  ``meta.json`` untouched);
- ``cross_checkpoint.json`` is byte-identical before/after a repair;
- a clean / non-cross run is a no-op.

All artifacts are built on disk in a tmp dir; no real pipeline is run.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.run_state import repair_cross_run_state, validate_cross_run_state


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _meta(run_dir: Path, payload: dict) -> None:
    _write(run_dir / "meta.json", payload)


def _ckpt(run_dir: Path, payload: dict) -> None:
    _write(run_dir / "cross_checkpoint.json", payload)


def _meta_dict(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


# ── stale terminal handoff: dry-run + apply + idempotency ──────────────


def test_dry_run_reports_change_without_writing(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done", "phase_handoff": {"id": "cfa:1"}})
    _ckpt(tmp_path, {"sub_status": {"web": "done"}})
    meta_before = (tmp_path / "meta.json").read_bytes()

    report = repair_cross_run_state(tmp_path)  # apply=False default

    assert report.applied is False
    assert len(report.changes) == 1
    change = report.changes[0]
    assert change.field == "phase_handoff"
    assert change.after is None
    assert change.issue_code == "cross_terminal_with_stale_handoff"
    assert "cross_terminal_with_stale_handoff" in report.issue_codes
    # Nothing written on a dry run.
    assert (tmp_path / "meta.json").read_bytes() == meta_before
    assert not (tmp_path / "run_state_repairs").exists()


def test_apply_clears_stale_handoff_with_backup_and_audit(
    tmp_path: Path,
) -> None:
    _meta(
        tmp_path,
        {"status": "failed", "halt_reason": "x", "phase_handoff": {"id": "cfa:1"}},
    )
    _ckpt(tmp_path, {"sub_status": {"web": "done"}})

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is True
    # meta.phase_handoff cleared; other fields preserved.
    meta = _meta_dict(tmp_path)
    assert "phase_handoff" not in meta
    assert meta["status"] == "failed"
    assert meta["halt_reason"] == "x"
    # Diagnosis is clean again.
    assert validate_cross_run_state(tmp_path) == ()
    # Backup + audit live under run_state_repairs/.
    assert report.backup_path is not None and report.backup_path.is_file()
    assert report.audit_path is not None and report.audit_path.is_file()
    repairs_dir = tmp_path / "run_state_repairs"
    assert repairs_dir.is_dir()
    audit = json.loads(report.audit_path.read_text(encoding="utf-8"))
    # issue_codes records every diagnosed cross code; the single applied change
    # is attributed to the one repairable code.
    assert "cross_terminal_with_stale_handoff" in audit["issue_codes"]
    assert audit["changes"] == [
        {
            "field": "phase_handoff",
            "before": {"id": "cfa:1"},
            "after": None,
            "issue_code": "cross_terminal_with_stale_handoff",
        }
    ]
    # The backup preserves the original (pre-repair) handoff.
    backup = json.loads(report.backup_path.read_text(encoding="utf-8"))
    assert backup["phase_handoff"] == {"id": "cfa:1"}


def test_second_apply_is_noop(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done", "phase_handoff": {"id": "cfa:1"}})

    first = repair_cross_run_state(tmp_path, apply=True)
    assert first.applied is True
    audits_after_first = sorted((tmp_path / "run_state_repairs").iterdir())

    second = repair_cross_run_state(tmp_path, apply=True)
    assert second.applied is False
    assert second.changes == ()
    # No new backup / audit artifact written.
    assert sorted((tmp_path / "run_state_repairs").iterdir()) == audits_after_first


# ── safety guard: stale terminal whose checkpoint is still pending ─────


def test_terminal_stale_with_checkpoint_pending_is_diagnostic(
    tmp_path: Path,
) -> None:
    # Torn shape: terminal meta still carries an active handoff AND the
    # checkpoint still flags it pending. Pre-repair the validator sees only the
    # repairable warning, but clearing meta.phase_handoff alone (the repair
    # never mutates cross_checkpoint.json) would surface the
    # checkpoint_pending_without_active_handoff ERROR. The safe repair must
    # refuse and defer to an operator decision.
    _meta(tmp_path, {"status": "done", "phase_handoff": {"id": "cfa:1"}})
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:1",
            "cfa_paused_state": {"verdict": "REJECTED"},
        },
    )
    # Sanity: pre-repair only the repairable warning is present (not the error).
    pre_codes = {i.code for i in validate_cross_run_state(tmp_path)}
    assert pre_codes == {"cross_terminal_with_stale_handoff"}

    meta_before = (tmp_path / "meta.json").read_bytes()
    ckpt_before = (tmp_path / "cross_checkpoint.json").read_bytes()

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is False
    assert report.changes == ()
    assert report.needs_operator_decision is True
    assert report.repair_hint is not None
    assert "checkpoint_pending_without_active_handoff" in report.repair_hint
    # Neither durable artifact is mutated, and no repair audit is written.
    assert (tmp_path / "meta.json").read_bytes() == meta_before
    assert (tmp_path / "cross_checkpoint.json").read_bytes() == ckpt_before
    assert not (tmp_path / "run_state_repairs").exists()
    # The run is not pushed into the more dangerous error shape.
    assert "checkpoint_pending_without_active_handoff" not in {
        i.code for i in validate_cross_run_state(tmp_path)
    }


# ── checkpoint immutability ────────────────────────────────────────────


def test_apply_never_mutates_cross_checkpoint(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "halted", "phase_handoff": {"id": "cfa:1"}})
    _ckpt(
        tmp_path,
        {
            "phase0_done": True,
            "sub_status": {"web": "done", "api": "failed"},
            "phase_handoff_kind": "cfa",
        },
    )
    ckpt_before = (tmp_path / "cross_checkpoint.json").read_bytes()

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is True
    # cross_checkpoint.json is byte-identical after the repair.
    assert (tmp_path / "cross_checkpoint.json").read_bytes() == ckpt_before


# ── ambiguous codes are strictly diagnostic ────────────────────────────


def _assert_diagnostic_only(
    run_dir: Path, expected_code: str, *, needs_decision: bool = False,
) -> None:
    """Apply must refuse: no change, no meta mutation, no checkpoint mutation."""
    meta_before = (run_dir / "meta.json").read_bytes()
    ckpt_before = (
        (run_dir / "cross_checkpoint.json").read_bytes()
        if (run_dir / "cross_checkpoint.json").exists()
        else None
    )

    report = repair_cross_run_state(run_dir, apply=True)

    assert report.applied is False
    assert report.changes == ()
    assert expected_code in report.issue_codes
    assert report.needs_operator_decision is needs_decision
    assert report.repair_hint is not None
    assert expected_code in report.repair_hint
    # Nothing written.
    assert (run_dir / "meta.json").read_bytes() == meta_before
    if ckpt_before is not None:
        assert (run_dir / "cross_checkpoint.json").read_bytes() == ckpt_before
    assert not (run_dir / "run_state_repairs").exists()


def test_checkpoint_pending_without_active_handoff_is_diagnostic(
    tmp_path: Path,
) -> None:
    _meta(tmp_path, {"status": "running"})
    _ckpt(
        tmp_path,
        {"phase_handoff_pending": True, "phase_handoff_kind": "plan"},
    )
    _assert_diagnostic_only(
        tmp_path,
        "checkpoint_pending_without_active_handoff",
        needs_decision=True,
    )


def test_active_handoff_without_checkpoint_pending_is_diagnostic(
    tmp_path: Path,
) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cross_plan:1"}},
    )
    _ckpt(tmp_path, {"phase_handoff_pending": False})
    _assert_diagnostic_only(
        tmp_path, "active_handoff_without_checkpoint_pending",
    )


def test_checkpoint_kind_id_mismatch_is_diagnostic(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cfa:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cfa:1",
        },
    )
    _assert_diagnostic_only(tmp_path, "checkpoint_kind_id_mismatch")


def test_project_handoff_marker_incomplete_is_diagnostic(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "project:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "project:1",
            "phase_handoff_child_id": "20260608_x",
            "sub_status": {"web": "running"},
            "phase_handoff_project_alias": "api",  # not in sub_status
        },
    )
    _assert_diagnostic_only(tmp_path, "project_handoff_marker_incomplete")


def test_cfa_pending_without_paused_state_is_diagnostic(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cfa:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:1",
        },
    )
    _assert_diagnostic_only(tmp_path, "cfa_pending_without_paused_state")


def test_pending_gate_and_handoff_active_is_diagnostic(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cross_plan:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cross_plan:1",
            "pending_gate": {"gate": "manual_confirm"},
        },
    )
    _assert_diagnostic_only(tmp_path, "pending_gate_and_handoff_active")


# ── no-op on clean / non-cross runs ────────────────────────────────────


def test_clean_cross_terminal_is_noop(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    _ckpt(tmp_path, {"sub_status": {"web": "done"}})

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is False
    assert report.changes == ()
    assert report.issue_codes == ()
    assert not (tmp_path / "run_state_repairs").exists()


def test_non_cross_clean_run_is_noop(tmp_path: Path) -> None:
    # No cross_checkpoint.json — a plain run. A clean terminal carries no
    # stale handoff, so there is nothing to repair.
    _meta(tmp_path, {"status": "done"})

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is False
    assert report.changes == ()
    assert not (tmp_path / "run_state_repairs").exists()
