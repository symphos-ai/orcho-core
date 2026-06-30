"""Unit tests for the opt-in run-state repair layer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.run_state import repair as repair_mod, repair_run_state

_REPAIRS_DIR = "run_state_repairs"


def _write_events(run_dir: Path, lines: list[dict]) -> None:
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _write_meta(run_dir: Path, meta: dict) -> None:
    run_dir.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _read_meta(run_dir: Path) -> dict:
    return json.loads(run_dir.joinpath("meta.json").read_text(encoding="utf-8"))


def _write_decision(run_dir: Path, name: str, decision: dict) -> None:
    dd = run_dir / "phase_handoff_decisions"
    dd.mkdir(exist_ok=True)
    dd.joinpath(f"{name}.json").write_text(json.dumps(decision), encoding="utf-8")


def _handoff_event(handoff_id: str, phase: str = "validate_plan", seq: int = 1) -> dict:
    return {
        "seq": seq,
        "ts": "t",
        "kind": "phase.handoff_requested",
        "phase": phase,
        "payload": {"handoff_id": handoff_id, "phase": phase},
    }


def _repair_files(run_dir: Path) -> list[Path]:
    repairs = run_dir / _REPAIRS_DIR
    if not repairs.is_dir():
        return []
    return sorted(repairs.iterdir())


def _audit_files(run_dir: Path) -> list[Path]:
    return [p for p in _repair_files(run_dir) if not p.name.endswith(".bak.json")]


def _backup_files(run_dir: Path) -> list[Path]:
    return [p for p in _repair_files(run_dir) if p.name.endswith(".bak.json")]


_HALT_DECIDED_AT = "2026-06-07T12:00:00+00:00"


def _torn_halt_run(run_dir: Path) -> None:
    """interrupted + active handoff + halt decision -> repairs to halted."""
    _write_events(run_dir, [_handoff_event("h1")])
    _write_meta(run_dir, {"status": "interrupted", "phase_handoff": {"id": "h1"}})
    _write_decision(
        run_dir,
        "h1",
        {"action": "halt", "handoff_id": "h1", "decided_at": _HALT_DECIDED_AT},
    )


def test_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(tmp_path, {"status": "halted", "phase_handoff": {"id": "h1"}})

    report = repair_run_state(tmp_path)  # apply defaults to False

    assert report.changes  # proposed at least one change
    assert report.applied is False
    assert report.backup_path is None
    assert report.audit_path is None
    assert report.repaired_at is None
    # meta.json untouched; repairs dir never created.
    assert _read_meta(tmp_path)["phase_handoff"] == {"id": "h1"}
    assert not (tmp_path / _REPAIRS_DIR).exists()


def test_apply_writes_backup_and_audit(tmp_path: Path) -> None:
    _torn_halt_run(tmp_path)

    report = repair_run_state(tmp_path, apply=True)

    meta = _read_meta(tmp_path)
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "phase_handoff_halt"
    # Full SDK post-halt shape: halted_at restored from the decision's
    # decided_at, not the repair timestamp.
    assert meta["halted_at"] == _HALT_DECIDED_AT
    assert "phase_handoff" not in meta

    assert report.applied is True
    assert "halt_decision_without_halted_meta" in report.issue_codes
    assert report.backup_path is not None and report.backup_path.is_file()
    assert report.audit_path is not None and report.audit_path.is_file()
    assert report.repaired_at is not None

    # exactly one backup + one audit on a single repairing apply.
    assert len(_backup_files(tmp_path)) == 1
    assert len(_audit_files(tmp_path)) == 1

    # backup preserves the original (pre-mutation) meta.
    backup = json.loads(report.backup_path.read_text(encoding="utf-8"))
    assert backup["status"] == "interrupted"
    assert backup["phase_handoff"] == {"id": "h1"}

    # audit records codes, changes, and relative paths.
    audit = json.loads(report.audit_path.read_text(encoding="utf-8"))
    assert "halt_decision_without_halted_meta" in audit["issue_codes"]
    changed_fields = {c["field"] for c in audit["changes"]}
    assert {"status", "halt_reason", "halted_at", "phase_handoff"} <= changed_fields
    halted_at_change = next(c for c in audit["changes"] if c["field"] == "halted_at")
    assert halted_at_change["after"] == _HALT_DECIDED_AT
    assert audit["backup_path"] == str(report.backup_path.relative_to(tmp_path))
    assert audit["audit_path"] == str(report.audit_path.relative_to(tmp_path))


def test_halt_repair_is_idempotent(tmp_path: Path) -> None:
    _torn_halt_run(tmp_path)

    first = repair_run_state(tmp_path, apply=True)
    assert first.applied is True
    files_after_first = len(_repair_files(tmp_path))

    second = repair_run_state(tmp_path, apply=True)
    assert second.applied is False
    assert second.changes == ()
    # no new backup / audit on the idempotent re-run.
    assert len(_repair_files(tmp_path)) == files_after_first


@pytest.mark.parametrize("status", ["halted", "done"])
def test_terminal_stale_handoff_repair_is_idempotent(
    tmp_path: Path, status: str
) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(tmp_path, {"status": status, "phase_handoff": {"id": "h1"}})

    first = repair_run_state(tmp_path, apply=True)
    assert first.applied is True
    meta = _read_meta(tmp_path)
    assert meta["status"] == status
    assert "phase_handoff" not in meta
    assert len(_audit_files(tmp_path)) == 1

    second = repair_run_state(tmp_path, apply=True)
    assert second.applied is False
    assert second.changes == ()
    assert len(_audit_files(tmp_path)) == 1


def test_interrupted_active_no_decision_is_refused(tmp_path: Path) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(tmp_path, {"status": "interrupted", "phase_handoff": {"id": "h1"}})

    report = repair_run_state(tmp_path, apply=True)

    assert report.needs_operator_decision is True
    assert report.changes == ()
    assert report.applied is False
    assert report.repair_hint is not None and "decide" in report.repair_hint
    # nothing written.
    assert _read_meta(tmp_path)["phase_handoff"] == {"id": "h1"}
    assert not (tmp_path / _REPAIRS_DIR).exists()


def test_meta_write_failure_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _torn_halt_run(tmp_path)

    def _boom(src, dst):  # noqa: ANN001
        raise OSError("simulated replace failure")

    monkeypatch.setattr(repair_mod.os, "replace", _boom)

    with pytest.raises(RuntimeError, match="atomically replace meta.json"):
        repair_run_state(tmp_path, apply=True)

    # original meta.json is still valid and unchanged (atomic replace).
    meta = _read_meta(tmp_path)
    assert meta["status"] == "interrupted"
    assert meta["phase_handoff"] == {"id": "h1"}

    # no leftover temp file in run_dir, and no audit artifact written.
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".meta.repair.")]
    assert leftover == []
    assert _audit_files(tmp_path) == []
