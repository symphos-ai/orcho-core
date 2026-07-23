# SPDX-License-Identifier: Apache-2.0
"""SDK contract tests for ``list_handoff_advice`` (Stage 0/1 advisor projection).

Pin the typed wrapper over
``pipeline.project.handoff_advice_evidence.collect_handoff_advice``: a run with
advice artifacts projects to typed ``calls`` + ``summary`` whose values match the
normalizer verbatim, and a run with no Stage 0/1 surface returns ``None`` without
raising. The wrapper adds no classification policy — these tests assert it is a
faithful, additive view, not a second classifier.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.project.handoff_advice_evidence import collect_handoff_advice
from sdk import (
    HandoffAdviceCall,
    HandoffAdviceEvidence,
    HandoffAdviceSummary,
    HandoffAdviceUsage,
    list_handoff_advice,
)

# ── fixtures / helpers ──────────────────────────────────────────────────────


def _seed_run(runs_dir: Path, run_id: str, *, meta: dict[str, Any]) -> Path:
    """Create a minimal run dir with meta.json + events.jsonl + metrics.json."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "metrics.json").write_text(
        json.dumps({
            "total_tokens": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_duration_s": 0.0,
            "total_rounds": 0,
        }) + "\n", encoding="utf-8",
    )
    return run_dir


def _write_advice(
    run_dir: Path,
    name: str,
    *,
    handoff_id: str = "review_changes:repair_round:1",
    phase: str = "review_changes",
    recommended_action: str = "retry_feedback",
    confidence: str = "high",
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
        "advice": {
            "recommended_action": recommended_action,
            "confidence": confidence,
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
    (advice_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    return f"phase_handoff_advice/{name}"


def _write_decision(
    run_dir: Path,
    name: str,
    *,
    advice_relpath: str,
    action: str = "retry_feedback",
    feedback_source: str = "agent_advice",
    phase: str = "review_changes",
    handoff_id: str = "review_changes:repair_round:1",
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
    (decisions_dir / name).write_text(json.dumps(payload), encoding="utf-8")


# ── no surface → None (the only stop condition) ─────────────────────────────


def test_list_handoff_advice_no_surface_returns_none(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260613_100000_aaaaaa", meta={"status": "done", "phases": {}})

    result = list_handoff_advice(
        "20260613_100000_aaaaaa", runs_dir=runs, cwd=None,
    )
    assert result is None


def test_list_handoff_advice_empty_advice_dir_returns_none(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(
        runs, "20260613_100000_bbbbbb", meta={"status": "done", "phases": {}},
    )
    (run_dir / "phase_handoff_advice").mkdir()

    result = list_handoff_advice(
        "20260613_100000_bbbbbb", runs_dir=runs, cwd=None,
    )
    assert result is None


# ── advice + decision → typed calls + summary matching the normalizer ───────


def test_list_handoff_advice_projects_resolved_retry(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    meta = {
        "status": "done",
        "phases": {
            "rounds": [{"round": 1, "critique": "P1: broken — fix the null check"}, {"round": 2, "critique": ""}],
        },
    }
    run_dir = _seed_run(runs, "20260613_100000_cccccc", meta=meta)
    rel = _write_advice(
        run_dir, "h1.json",
        usage={"tokens_in": 100, "tokens_out": 50,
               "tokens_in_cache_read": 3, "duration_s": 4.5},
    )
    _write_decision(run_dir, "d1.json", advice_relpath=rel)

    result = list_handoff_advice(
        "20260613_100000_cccccc", runs_dir=runs, cwd=None,
    )

    assert isinstance(result, HandoffAdviceEvidence)
    assert len(result.calls) == 1
    call = result.calls[0]
    assert isinstance(call, HandoffAdviceCall)
    assert call.handoff_id == "review_changes:repair_round:1"
    assert call.phase == "review_changes"
    assert call.advice_artifact == "phase_handoff_advice/h1.json"
    assert call.recommended_action == "retry_feedback"
    assert call.applied_action == "retry_feedback"
    assert call.feedback_source == "agent_advice"
    assert call.outcome == "resolved"
    assert call.resolved is True
    assert call.repeated is False
    assert call.trigger == "rejected"
    assert call.verdict == "REJECTED"
    assert call.confidence == "high"
    # Usage fields preserved per-call.
    assert call.tokens_in == 100
    assert call.tokens_out == 50
    assert call.tokens_cached == 3
    assert call.duration_s == 4.5
    assert call.cost_usd_equivalent is None
    assert call.model is None

    summary = result.summary
    assert isinstance(summary, HandoffAdviceSummary)
    assert summary.calls == 1
    assert summary.applied_retries == 1
    assert summary.resolved_retries == 1
    assert summary.repeated == 0
    assert summary.stopped == 0
    assert summary.unknown == 0
    assert isinstance(summary.usage, HandoffAdviceUsage)
    assert summary.usage.tokens_in == 100
    assert summary.usage.tokens_cached == 3
    assert summary.usage.duration_s == 4.5
    assert summary.usage.cost_usd_equivalent is None


def test_list_handoff_advice_unapplied_call_is_stopped(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(
        runs, "20260613_100000_dddddd", meta={"status": "done", "phases": {}},
    )
    _write_advice(run_dir, "h1.json")  # no matching decision

    result = list_handoff_advice(
        "20260613_100000_dddddd", runs_dir=runs, cwd=None,
    )

    assert result is not None
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.applied_action is None
    assert call.feedback_source is None
    assert call.outcome == "stopped"
    assert call.resolved is None
    assert call.repeated is False
    assert result.summary.stopped == 1
    assert result.summary.applied_retries == 0


# ── values match collect_handoff_advice verbatim (no second classifier) ─────


def test_list_handoff_advice_matches_normalizer_values(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    meta = {
        "status": "awaiting_phase_handoff",
        "phases": {
            "rounds": [
                {"round": 1, "critique": "P1: broken"},
                {"round": 2, "critique": "P1: still broken"},
            ],
        },
    }
    run_dir = _seed_run(runs, "20260613_100000_eeeeee", meta=meta)
    rel1 = _write_advice(
        run_dir, "h1.json", handoff_id="review_changes:repair_round:1",
        created_at="2026-06-13T10:00:00+00:00",
        usage={"tokens_in": 10, "tokens_out": 5, "cost_usd_equivalent": 0.01,
               "model": "claude"},
    )
    rel2 = _write_advice(
        run_dir, "h2.json", handoff_id="review_changes:repair_round:2",
        created_at="2026-06-13T10:05:00+00:00",
    )
    _write_decision(run_dir, "d1.json", advice_relpath=rel1,
                    handoff_id="review_changes:repair_round:1")
    _write_decision(run_dir, "d2.json", advice_relpath=rel2,
                    handoff_id="review_changes:repair_round:2")

    raw = collect_handoff_advice(run_dir, meta)
    assert raw is not None
    result = list_handoff_advice(
        "20260613_100000_eeeeee", runs_dir=runs, cwd=None,
    )
    assert result is not None

    # The projection is a faithful 1:1 view: outcome / resolved / repeated and the
    # summary counts equal the normalizer's, field-for-field.
    assert [c.outcome for c in result.calls] == [c["outcome"] for c in raw["calls"]]
    assert [c.resolved for c in result.calls] == [c["resolved"] for c in raw["calls"]]
    assert [c.repeated for c in result.calls] == [c["repeated"] for c in raw["calls"]]
    assert [c.handoff_id for c in result.calls] == [
        c["handoff_id"] for c in raw["calls"]
    ]
    assert result.summary.calls == raw["summary"]["calls"]
    assert result.summary.applied_retries == raw["summary"]["applied_retries"]
    assert result.summary.resolved_retries == raw["summary"]["resolved_retries"]
    assert result.summary.repeated == raw["summary"]["repeated"]
    assert result.summary.unknown == raw["summary"]["unknown"]
    # The first call carried model + cost accounting — preserved verbatim.
    first = result.calls[0]
    assert first.model == "claude"
    assert first.cost_usd_equivalent == 0.01


def test_list_handoff_advice_decision_only_surface_empty_calls(tmp_path: Path) -> None:
    """A decision carrying advice provenance keeps the surface present (no None),
    with no advice artifacts to build calls from — projected as empty calls."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(
        runs, "20260613_100000_ffffff", meta={"status": "done", "phases": {}},
    )
    _write_decision(
        run_dir, "d1.json",
        advice_relpath="phase_handoff_advice/h1.json",
        feedback_source="ci_agent",
    )

    result = list_handoff_advice(
        "20260613_100000_ffffff", runs_dir=runs, cwd=None,
    )
    assert result is not None
    assert result.calls == ()
    assert result.summary.calls == 0
    assert result.summary.usage is None
