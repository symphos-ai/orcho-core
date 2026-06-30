"""Cross-terminal stale-handoff clearing (Stage 7 / T2).

Pins the load-bearing fix: every in-scope cross terminal
(``halted`` / ``cancelled`` / ``done`` / ``failed``) routes its status
field-mutation through the cross-safe
:func:`pipeline.run_state.terminal.settle_cross_terminal`, so the persisted
``meta.json`` carries NO stale active ``phase_handoff``. The failed path is
called out explicitly (review F1) because the single-project writer would
preserve the handoff — the cross path must not.

The regression seam re-confirms the finalization side effects are unchanged:
``run.end`` fires exactly once and ``session.json`` (meta.json) is written
once.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline.cross_project.finalization import (
    CrossFinalizationContext,
    finalize_cross_run,
)
from pipeline.cross_project.terminal import finalize_cross_terminal


def _read_meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def _session_with_handoff(handoff_id: str) -> dict:
    return {
        "run_id": "TEST_RUN",
        "status": "awaiting_phase_handoff",
        "phase_handoff": {
            "id": handoff_id,
            "phase": "cross_final_acceptance",
            "available_actions": ["continue", "halt"],
        },
    }


def _make_cfa(*, approved: bool, source: str = "agent") -> SimpleNamespace:
    return SimpleNamespace(
        parsed=SimpleNamespace(approved=approved), source=source,
    )


def _ctx(run_dir: Path, session: dict, **kw) -> CrossFinalizationContext:
    return CrossFinalizationContext(
        run_dir=run_dir,
        output_dir=kw.get("output_dir", True),
        session=session,
        projects=kw.get("projects", {"api": Path("/tmp/api")}),
        max_rounds=kw.get("max_rounds", 2),
        cfa_result=kw.get("cfa_result"),
        contract_results=kw.get("contract_results", {}),
        contract_check_failed=kw.get("contract_check_failed", False),
        contract_check_failure_reason=kw.get(
            "contract_check_failure_reason"
        ),
        cross_phase_usage=kw.get("cross_phase_usage", {}),
    )


# ── (a) halt from an active cross pause clears the stale handoff ────────


@pytest.mark.parametrize(
    "handoff_id",
    ["cross_plan:1", "cfa:1", "project:1"],
)
def test_halt_from_cross_pause_clears_handoff(
    tmp_path: Path, handoff_id: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_handoff(handoff_id)

    finalize_cross_terminal(
        run_dir=run_dir,
        session=session,
        status="halted",
        halt_reason="phase_handoff_halt",
    )

    meta = _read_meta(run_dir)
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "phase_handoff_halt"
    assert "phase_handoff" not in meta
    # The in-memory session was mutated too (run_dir-less callers rely on it).
    assert "phase_handoff" not in session


# ── (d) cancelled (contract_check ABORT) clears handoff, sets reason ────


def test_cancelled_abort_clears_handoff_and_sets_reason(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_handoff("project:1")

    # ABORT path passes no explicit halt_reason → cross_<status> fallback.
    finalize_cross_terminal(
        run_dir=run_dir, session=session, status="cancelled",
    )

    meta = _read_meta(run_dir)
    assert meta["status"] == "cancelled"
    assert meta["halt_reason"] == "cross_cancelled"
    assert "phase_handoff" not in meta


def test_finalize_cross_terminal_preserves_preset_halt_reason(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_handoff("cfa:1")
    session["halt_reason"] = "pre_set_reason"

    finalize_cross_terminal(
        run_dir=run_dir, session=session, status="failed",
    )

    meta = _read_meta(run_dir)
    assert meta["halt_reason"] == "pre_set_reason"
    assert "phase_handoff" not in meta


def test_finalize_cross_terminal_run_dir_none_only_mutates() -> None:
    session = _session_with_handoff("cfa:1")
    finalize_cross_terminal(
        run_dir=None, session=session, status="halted",
        halt_reason="phase_handoff_halt",
    )
    assert session["status"] == "halted"
    assert session["halt_reason"] == "phase_handoff_halt"
    assert "phase_handoff" not in session


# ── (b) finalize_cross_run done clears a residual handoff ───────────────


def test_finalize_cross_run_done_clears_residual_handoff(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_handoff("cfa:1")
    session["phases"] = {"projects": {}}
    ctx = _ctx(run_dir, session, cfa_result=_make_cfa(approved=True))

    with patch(
        "pipeline.engine.artifact_mirror.mirror_to_projects", return_value=[],
    ):
        result = finalize_cross_run(ctx)

    assert result.status == "done"
    meta = _read_meta(run_dir)
    assert meta["status"] == "done"
    assert "phase_handoff" not in meta


# ── (c) finalize_cross_run FAILED clears a residual handoff (F1) ────────


def test_finalize_cross_run_failed_clears_residual_handoff(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_handoff("cfa:1")
    session["phases"] = {"projects": {}}
    # CFA REJECTED → failed terminal. The single-project mark_run_failed
    # would preserve the handoff; the cross path must clear it.
    ctx = _ctx(run_dir, session, cfa_result=_make_cfa(approved=False))

    with patch(
        "pipeline.engine.artifact_mirror.mirror_to_projects", return_value=[],
    ):
        result = finalize_cross_run(ctx)

    assert result.status == "failed"
    meta = _read_meta(run_dir)
    assert meta["status"] == "failed"
    assert meta["halt_reason"] == "cross_final_acceptance_failed"
    assert "phase_handoff" not in meta


# ── (e) regression: run.end once, single session/metrics/evidence write ─


def test_finalize_cross_run_side_effects_unchanged(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_handoff("cfa:1")
    session["phases"] = {"projects": {}}
    # cross_phase_usage is non-empty so the metrics.json write branch fires —
    # otherwise the metrics guard below would be vacuous.
    ctx = _ctx(
        run_dir,
        session,
        cfa_result=_make_cfa(approved=True),
        cross_phase_usage={"plan": {"total_cost": 1.0}},
    )

    emitted: list[dict] = []

    def _capture_emit(kind: str, **fields):
        emitted.append({"kind": kind, **fields})

    with (
        patch(
            "core.observability.events.emit", side_effect=_capture_emit,
        ),
        patch(
            "pipeline.engine.artifact_mirror.mirror_to_projects",
            return_value=[],
        ),
        patch(
            "pipeline.cross_project.finalization.save_cross_session",
        ) as save_mock,
        patch(
            "core.observability.metrics.cross_metrics_dict",
            return_value={"total_cost": 1.0},
        ) as metrics_mock,
        patch(
            "pipeline.evidence.write_bundle_or_placeholder",
        ) as evidence_mock,
    ):
        finalize_cross_run(ctx)

    run_end = [e for e in emitted if e["kind"] == "run.end"]
    assert len(run_end) == 1
    assert run_end[0]["status"] == "done"
    # Exactly one session.json, one metrics.json rollup, one evidence bundle.
    assert save_mock.call_count == 1
    assert metrics_mock.call_count == 1
    assert evidence_mock.call_count == 1
    # The metrics.json file was materialised exactly once on disk.
    assert (run_dir / "metrics.json").is_file()
