# SPDX-License-Identifier: Apache-2.0
"""F2 upper-layer integration: durable advice usage → metrics.json (observe-only).

Proves the layered T4 contract end-to-end at the upper layer: a synthetic run
dir carrying advice artifacts (with usage) flows through the REAL push code
(``pipeline.project.run._push_handoff_advice_usage`` → ``collect_handoff_advice``
→ ``MetricsCollector.record_advice_usage``) and lands in ``metrics.json`` under the
additive ``handoff_advice`` slot — WITHOUT changing ``total_*``. ``core`` never
imports ``pipeline.project``; the usage is normalized in the upper layer and
handed to the collector as primitives.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.observability.metrics import MetricsCollector
from pipeline.project.run import _push_handoff_advice_usage

# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def accounting_on(monkeypatch: pytest.MonkeyPatch):
    from core.infra import config
    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    yield
    config._reset_config()


def _write_advice(
    run_dir: Path,
    name: str,
    *,
    handoff_id: str = "h1",
    phase: str = "review_changes",
    recommended_action: str = "retry_feedback",
    usage: dict[str, Any] | None = None,
    created_at: str = "2026-06-13T10:00:00+00:00",
) -> str:
    advice_dir = run_dir / "phase_handoff_advice"
    advice_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": "r1",
        "handoff_id": handoff_id,
        "phase": phase,
        "created_at": created_at,
        "response_language": "",
        "advice": {
            "recommended_action": recommended_action,
            "confidence": "high",
            "rationale": "because",
            "retry_feedback": "fix it",
            "risks": [],
            "expected_files": [],
            "operator_note": "",
            "parse_warnings": [],
        },
        "raw_output": "",
        "usage": usage if usage is not None else {},
    }
    advice_dir.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")
    return f"phase_handoff_advice/{name}"


def _write_decision(
    run_dir: Path,
    name: str,
    *,
    advice_relpath: str,
    feedback_source: str = "agent_advice",
    action: str = "retry_feedback",
    phase: str = "review_changes",
    handoff_id: str = "h1",
) -> None:
    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    note = f"feedback_source={feedback_source}; advice_artifact={advice_relpath}"
    payload = {
        "run_id": "r1",
        "handoff_id": handoff_id,
        "phase": phase,
        "action": action,
        "feedback": "fix it",
        "note": note,
        "decided_at": "2026-06-13T10:05:00+00:00",
    }
    decisions_dir.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")


def _session() -> dict[str, Any]:
    """Meta-form session (the 'phases' mapping collect_handoff_advice expects)."""
    return {
        "status": "done",
        "phases": {
            "review_changes": [
                {"attempt": 1, "approved": False, "verdict": "REJECTED",
                 "findings": [{"id": "F1", "severity": "P1", "title": "bug"}]},
                {"attempt": 2, "approved": True, "verdict": "APPROVED",
                 "findings": []},
            ],
        },
    }


def _fake_run(run_dir: Path, session: dict[str, Any], metrics: MetricsCollector):
    """Minimal duck-typed run carrying just what the push method reads."""
    return SimpleNamespace(output_dir=run_dir, session=session, _metrics=metrics)


def _push(run: Any) -> None:
    """Invoke the REAL upper-layer push code path (the ``_fsm_metrics`` hook)."""
    _push_handoff_advice_usage(run)


# ── tests ───────────────────────────────────────────────────────────────────


def test_durable_advice_usage_fills_metrics_without_changing_totals(
    accounting_on, tmp_path: Path,
) -> None:
    relpath = _write_advice(
        tmp_path, "h1.json",
        usage={"tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01},
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)

    metrics = MetricsCollector(default_model="mock")
    # A real pipeline phase establishes the authoritative totals.
    metrics.record_phase(
        "review_changes", tokens_in=3000, tokens_out=300, cost_usd=0.06,
    )
    before = metrics.as_dict()

    _push(_fake_run(tmp_path, _session(), metrics))

    out_path = metrics.save(tmp_path)
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))

    # handoff_advice slot filled from the durable artifact usage ...
    assert data["handoff_advice"]["tokens_in"] == 800
    assert data["handoff_advice"]["tokens_out"] == 200
    assert data["handoff_advice"]["cost_usd_equivalent"] == 0.01
    # ... and the authoritative totals are byte-for-byte unchanged.
    assert data["total_tokens"] == before["total_tokens"] == 3300
    assert data["total_tokens_in"] == before["total_tokens_in"] == 3000
    assert data["total_tokens_out"] == before["total_tokens_out"] == 300
    assert data["total_cost_usd_equivalent"] == before["total_cost_usd_equivalent"]
    # Advice usage is NOT a pipeline phase.
    assert "handoff_advice" not in data["phases"]


def test_metrics_preserves_cached_tokens_and_duration(
    accounting_on, tmp_path: Path,
) -> None:
    # F2: cache-read tokens and duration must survive the summary aggregation
    # and reach metrics.json['handoff_advice'] (task: record per-call AND
    # aggregate cached tokens / duration when known).
    relpath = _write_advice(
        tmp_path, "h1.json",
        usage={"tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01,
               "tokens_in_cache_read": 3, "duration_s": 4.5},
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)

    metrics = MetricsCollector(default_model="mock")
    metrics.record_phase("review_changes", tokens_in=3000, tokens_out=300)
    before = metrics.as_dict()

    _push(_fake_run(tmp_path, _session(), metrics))

    out_path = metrics.save(tmp_path)
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))

    advice = data["handoff_advice"]
    assert advice["tokens_in"] == 800
    assert advice["tokens_cached"] == 3
    assert advice["duration_s"] == 4.5
    # Totals stay authoritative + unchanged.
    assert data["total_tokens"] == before["total_tokens"] == 3300
    assert "handoff_advice" not in data["phases"]


def test_cost_omitted_without_accounting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from core.infra import config
    monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
    config._reset_config()
    try:
        relpath = _write_advice(
            tmp_path, "h1.json",
            usage={"tokens_in": 800, "tokens_out": 200,
                   "cost_usd_equivalent": 0.01},
        )
        _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
        metrics = MetricsCollector(default_model="mock")
        _push(_fake_run(tmp_path, _session(), metrics))
        data = metrics.as_dict()
        # Tokens surface, cost is suppressed (never an invented dollar figure).
        assert data["handoff_advice"]["tokens_in"] == 800
        assert "cost_usd_equivalent" not in data["handoff_advice"]
    finally:
        config._reset_config()


def test_no_advice_surface_leaves_metrics_untouched(tmp_path: Path) -> None:
    metrics = MetricsCollector(default_model="mock")
    metrics.record_phase("review_changes", tokens_in=10, tokens_out=2)
    _push(_fake_run(tmp_path, {"status": "done", "phases": {}}, metrics))
    assert "handoff_advice" not in metrics.as_dict()


def test_advice_artifact_without_usage_records_nothing(tmp_path: Path) -> None:
    # Advice artifact present but no usage in it → usage unavailable → no slot.
    relpath = _write_advice(tmp_path, "h1.json", usage={})
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
    metrics = MetricsCollector(default_model="mock")
    _push(_fake_run(tmp_path, _session(), metrics))
    assert "handoff_advice" not in metrics.as_dict()


def test_no_output_dir_is_a_noop(tmp_path: Path) -> None:
    metrics = MetricsCollector(default_model="mock")
    _push(_fake_run(None, _session(), metrics))  # output_dir=None
    assert "handoff_advice" not in metrics.as_dict()


# ── F2: finalization backstop covers advice with NO following phase-end ──────


def test_finalization_backstop_records_stopped_advice_usage(
    accounting_on, tmp_path: Path,
) -> None:
    """An advice call with usage but no applied decision and NO following phase
    (an operator/CI stop / non-retry / menu-return) never reaches the per-phase
    push. The finalize-time backstop must still fold its usage into the
    observe-only metrics slot from the durable artifacts, without touching
    total_*."""
    from pipeline.project.finalization import _record_advice_usage_backstop

    # Unapplied advice (no decision) WITH usage — outcome 'stopped'.
    _write_advice(
        tmp_path, "h1.json",
        usage={"tokens_in": 120, "tokens_out": 30, "cost_usd_equivalent": 0.02},
    )
    metrics = MetricsCollector(default_model="mock")
    metrics.record_phase(
        "review_changes", tokens_in=3000, tokens_out=300, cost_usd=0.06,
    )
    before = metrics.as_dict()

    # No per-phase push happened (run stopped). The finalize backstop runs.
    _record_advice_usage_backstop(_fake_run(tmp_path, _session(), metrics))

    data = metrics.as_dict()
    assert data["handoff_advice"]["tokens_in"] == 120
    assert data["handoff_advice"]["tokens_out"] == 30
    assert data["handoff_advice"]["cost_usd_equivalent"] == 0.02
    # Totals authoritative + unchanged.
    assert data["total_tokens"] == before["total_tokens"] == 3300
    assert data["total_cost_usd_equivalent"] == before["total_cost_usd_equivalent"]
    assert "handoff_advice" not in data["phases"]


def test_finalization_backstop_noop_without_advice(tmp_path: Path) -> None:
    from pipeline.project.finalization import _record_advice_usage_backstop

    metrics = MetricsCollector(default_model="mock")
    metrics.record_phase("review_changes", tokens_in=10, tokens_out=2)
    _record_advice_usage_backstop(
        _fake_run(tmp_path, {"status": "halted", "phases": {}}, metrics),
    )
    assert "handoff_advice" not in metrics.as_dict()
