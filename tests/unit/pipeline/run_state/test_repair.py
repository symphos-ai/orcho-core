"""Unit tests for the opt-in run-state repair layer."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.run_state import (
    consistency as consistency_mod,
    repair as repair_mod,
    repair_run_state,
)
from pipeline.run_state.consistency import validate_run_state

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
_ORPHAN_CODE = "running_without_live_process"


def _torn_halt_run(run_dir: Path) -> None:
    """interrupted + active handoff + halt decision -> repairs to halted."""
    _write_events(run_dir, [_handoff_event("h1")])
    _write_meta(run_dir, {"status": "interrupted", "phase_handoff": {"id": "h1"}})
    _write_decision(
        run_dir,
        "h1",
        {"action": "halt", "handoff_id": "h1", "decided_at": _HALT_DECIDED_AT},
    )


def _orphaned_running_run(
    run_dir: Path,
    *,
    status: str = "running",
    event_at: datetime | None = None,
    started_at: datetime | None = None,
    terminal_event: str | None = None,
    include_handoff: bool = False,
) -> None:
    now = datetime.now(UTC)
    event_time = event_at or now - timedelta(minutes=5)
    launch_time = started_at or now - timedelta(minutes=5)
    meta = {"status": status}
    if include_handoff:
        meta["phase_handoff"] = {"id": "h1"}
    _write_meta(run_dir, meta)
    events: list[dict] = [
        {
            "seq": 1,
            "ts": event_time.isoformat(),
            "kind": "run.start",
            "payload": {},
        }
    ]
    if include_handoff:
        events.append({
            "seq": 2,
            "ts": event_time.isoformat(),
            "kind": "phase.handoff_requested",
            "payload": {"handoff_id": "h1", "phase": "validate_plan"},
        })
    if terminal_event:
        events.append({
            "seq": len(events) + 1,
            "ts": event_time.isoformat(),
            "kind": terminal_event,
            "payload": {},
        })
    _write_events(run_dir, events)
    run_dir.joinpath("run_supervisor.json").write_text(
        json.dumps({"pid": 4321, "started_at": launch_time.isoformat()}),
        encoding="utf-8",
    )


def _dead_pid(_pid: int) -> bool:
    return False


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


def test_orphaned_running_dry_run_reports_three_canonical_changes(tmp_path: Path) -> None:
    _orphaned_running_run(tmp_path)
    original = _read_meta(tmp_path)

    report = repair_run_state(tmp_path, pid_probe=_dead_pid)

    assert report.applied is False
    assert report.issue_codes == (_ORPHAN_CODE,)
    changes = {change.field: change for change in report.changes}
    assert set(changes) == {"status", "interrupted_at", "halt_reason"}
    assert changes["status"].after == "interrupted"
    assert changes["interrupted_at"].after is not None
    assert changes["halt_reason"].after == "interrupted_orphan"
    assert {change.issue_code for change in changes.values()} == {_ORPHAN_CODE}
    assert _read_meta(tmp_path) == original
    assert not (tmp_path / _REPAIRS_DIR).exists()


def test_orphaned_running_apply_is_idempotent_and_keeps_active_handoff(tmp_path: Path) -> None:
    _orphaned_running_run(tmp_path, include_handoff=True)

    first = repair_run_state(tmp_path, apply=True, pid_probe=_dead_pid)

    meta = _read_meta(tmp_path)
    assert first.applied is True
    assert meta["status"] == "interrupted"
    assert meta["halt_reason"] == "interrupted_orphan"
    assert meta["interrupted_at"] == first.repaired_at
    assert meta["phase_handoff"] == {"id": "h1"}
    assert len(_backup_files(tmp_path)) == 1
    assert len(_audit_files(tmp_path)) == 1
    audit = json.loads(first.audit_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert _ORPHAN_CODE in audit["issue_codes"]
    assert {change["field"] for change in audit["changes"]} == {
        "status", "interrupted_at", "halt_reason",
    }

    bytes_after_first = (tmp_path / "meta.json").read_bytes()
    second = repair_run_state(tmp_path, apply=True, pid_probe=_dead_pid)

    assert second.applied is False
    assert second.changes == ()
    assert (tmp_path / "meta.json").read_bytes() == bytes_after_first
    assert len(_backup_files(tmp_path)) == 1
    assert len(_audit_files(tmp_path)) == 1


def test_orphan_repair_noops_for_alive_fresh_terminal_parked_and_bad_facts(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    cases = [
        ("alive", {"pid_probe": lambda _pid: True}),
        ("fresh", {"event_at": now, "started_at": now - timedelta(minutes=5)}),
        ("terminal-event", {"terminal_event": "run.end"}),
        ("terminal-status", {"status": "done"}),
        ("parked", {"status": "awaiting_phase_handoff"}),
        ("bad-launch", {"started_at": None}),
    ]
    for name, options in cases:
        run_dir = tmp_path / name
        run_dir.mkdir()
        _orphaned_running_run(
            run_dir,
            status=options.get("status", "running"),  # type: ignore[arg-type]
            event_at=options.get("event_at"),  # type: ignore[arg-type]
            started_at=options.get("started_at"),  # type: ignore[arg-type]
            terminal_event=options.get("terminal_event"),  # type: ignore[arg-type]
        )
        if name == "bad-launch":
            run_dir.joinpath("run_supervisor.json").write_text(
                json.dumps({"pid": "bad", "started_at": "not-a-time"}), encoding="utf-8"
            )
        report = repair_run_state(
            run_dir, pid_probe=options.get("pid_probe", _dead_pid)  # type: ignore[arg-type]
        )
        assert report.applied is False, name
        assert report.changes == (), name
        assert _ORPHAN_CODE not in report.issue_codes, name
        assert not (run_dir / _REPAIRS_DIR).exists(), name


def test_orphan_repair_noops_when_pid_probe_errors(tmp_path: Path) -> None:
    _orphaned_running_run(tmp_path)

    def _probe_error(_pid: int) -> bool:
        raise OSError("probe unavailable")

    report = repair_run_state(tmp_path, pid_probe=_probe_error)

    assert report.changes == ()
    assert _ORPHAN_CODE not in report.issue_codes
    assert not (tmp_path / _REPAIRS_DIR).exists()


def test_validate_run_state_never_calls_repair_pid_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orphaned_running_run(tmp_path)
    called = False

    def _probe(_pid: int) -> bool:
        nonlocal called
        called = True
        raise AssertionError("validate_run_state must not probe process liveness")

    monkeypatch.setattr(repair_mod, "pid_is_alive", _probe)

    report = validate_run_state(tmp_path)

    assert called is False
    assert _ORPHAN_CODE not in {issue.code for issue in report.issues}
    source = Path(consistency_mod.__file__).read_text(encoding="utf-8")
    assert "run_supervisor.json" not in source
    assert "pid_is_alive" not in source


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
