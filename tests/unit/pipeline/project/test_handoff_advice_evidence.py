# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``pipeline.project.handoff_advice_evidence.collect_handoff_advice``.

These exercise the leaf normalizer against synthetic durable artifacts (real
temp run dirs, hand-written advice/decision JSON, mock ``usage``). No real
provider is ever invoked and no other ``pipeline.project`` module is required at
runtime — the normalizer only reads artifacts and ``meta['phases']``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.project.handoff_advice_evidence import collect_handoff_advice

# ── fixtures / helpers ──────────────────────────────────────────────────────


def _write_advice(
    run_dir: Path,
    name: str,
    *,
    handoff_id: str = "h1",
    phase: str = "review_changes",
    recommended_action: str = "retry_feedback",
    confidence: str = "high",
    usage: dict[str, Any] | None = None,
    created_at: str = "2026-06-13T10:00:00+00:00",
) -> str:
    """Write a synthetic advice artifact; return its phase-relative relpath."""
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
    action: str = "retry_feedback",
    advice_relpath: str,
    feedback_source: str = "agent_advice",
    phase: str = "review_changes",
    handoff_id: str = "h1",
) -> None:
    """Write a synthetic decision artifact whose note references the advice."""
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


def _finding(fid: str, severity: str, title: str) -> dict[str, Any]:
    return {"id": fid, "severity": severity, "title": title}


def _meta(phases: dict[str, Any]) -> dict[str, Any]:
    return {"status": "done", "phases": phases}


# ── stop condition (F1, narrow) ─────────────────────────────────────────────


def test_no_surface_returns_none(tmp_path: Path) -> None:
    """Entirely absent Stage 0/1 surface → None (the only stop condition)."""
    assert collect_handoff_advice(tmp_path, _meta({})) is None


def test_empty_advice_dir_no_decisions_returns_none(tmp_path: Path) -> None:
    """An empty advice directory with no advice provenance is still no surface."""
    (tmp_path / "phase_handoff_advice").mkdir()
    assert collect_handoff_advice(tmp_path, _meta({})) is None


# ── unapplied advice (acceptance criterion 2) ───────────────────────────────


def test_advice_without_decision_is_unapplied_call_not_none(tmp_path: Path) -> None:
    """Advice artifact present, no matching decision → one call, applied=None."""
    _write_advice(tmp_path, "h1.json")
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["applied_action"] is None
    assert call["feedback_source"] is None
    assert call["outcome"] == "stopped"
    assert call["resolved"] is None
    assert call["repeated"] is False
    assert result["summary"]["calls"] == 1
    assert result["summary"]["stopped"] == 1
    assert result["summary"]["applied_retries"] == 0


# ── resolved retries (both sources) — forward-compat STRUCTURED shape ────────
#
# These feed a structured ``phases['review_changes']`` attempt list (verdict +
# findings per attempt). The live loop does not persist this shape today (it
# writes text-only ``rounds`` — covered by the "REAL persisted …" block below),
# but the classifier honours it should a future adapter add one, and
# validate_plan / final_acceptance use exactly this fingerprint path.


def test_agent_advice_resolved_after_approved_review(tmp_path: Path) -> None:
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(
        tmp_path, "d1.json", advice_relpath=relpath, feedback_source="agent_advice",
    )
    meta = _meta({
        "review_changes": [
            {"attempt": 1, "approved": False, "verdict": "REJECTED",
             "findings": [_finding("F1", "P1", "bug")]},
            {"attempt": 2, "approved": True, "verdict": "APPROVED", "findings": []},
        ],
    })
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["applied_action"] == "retry_feedback"
    assert call["feedback_source"] == "agent_advice"
    assert call["outcome"] == "resolved"
    assert call["resolved"] is True
    assert call["repeated"] is False
    assert call["verdict"] == "REJECTED"
    assert call["trigger"] == "rejected"
    assert result["summary"]["resolved_retries"] == 1
    assert result["summary"]["applied_retries"] == 1


def test_ci_agent_resolved_after_approved_review(tmp_path: Path) -> None:
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(
        tmp_path, "d1.json", advice_relpath=relpath, feedback_source="ci_agent",
    )
    meta = _meta({
        "review_changes": [
            {"attempt": 1, "approved": False, "findings": [_finding("F1", "P2", "x")]},
            {"attempt": 2, "approved": True, "findings": []},
        ],
    })
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["feedback_source"] == "ci_agent"
    assert call["outcome"] == "resolved"


# ── repeated finding (conservative) ─────────────────────────────────────────


def test_repeated_p1_finding_after_retry_is_repeated_not_resolved(
    tmp_path: Path,
) -> None:
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
    same = _finding("F1", "P1", "still broken")
    meta = _meta({
        "review_changes": [
            {"attempt": 1, "approved": False, "verdict": "REJECTED",
             "findings": [same]},
            {"attempt": 2, "approved": False, "verdict": "REJECTED",
             "findings": [dict(same)]},
        ],
    })
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["outcome"] == "repeated"
    assert call["repeated"] is True
    assert call["resolved"] is False
    assert result["summary"]["repeated"] == 1
    assert result["summary"]["resolved_retries"] == 0


def test_applied_retry_without_next_attempt_is_unknown(tmp_path: Path) -> None:
    """Run ended before the next verdict → unknown, not resolved."""
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
    meta = _meta({
        "review_changes": [
            {"attempt": 1, "approved": False, "findings": [_finding("F1", "P1", "x")]},
        ],
    })
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    assert result["calls"][0]["outcome"] == "unknown"
    assert result["calls"][0]["resolved"] is None


# ── REAL persisted review/repair-loop shape (F1) ────────────────────────────
#
# The live review/repair loop persists to ``meta['phases']['rounds']`` via
# RoundAdapter (text-only round entries: a per-round ``critique`` — blank ⟺ the
# review approved — and a ``round`` number; NO structured findings, and NO
# ``phases['review_changes']`` key). The tests above exercise the forward-compat
# structured shape; these cover the shape a real run actually writes, so the
# resolved/repeated classification is not silently driven to ``unknown``.


def _rounds_meta(rounds: list[dict[str, Any]], *, status: str,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    phases: dict[str, Any] = {"rounds": rounds}
    if extra:
        phases.update(extra)
    return {"status": status, "phases": phases}


def test_real_rounds_shape_resolved_via_terminal_status(tmp_path: Path) -> None:
    # review_changes advice retry, then the run reached DONE — the pipeline only
    # completes once the review loop approved, so the retry resolved.
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(
        tmp_path, "d1.json", advice_relpath=relpath, feedback_source="agent_advice",
        phase="review_changes",
    )
    meta = _rounds_meta(
        [{"round": 1, "critique": "P1: still broken — fix the null check"}],
        status="done",
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["feedback_source"] == "agent_advice"
    assert call["applied_action"] == "retry_feedback"
    assert call["outcome"] == "resolved"
    assert call["resolved"] is True
    assert call["repeated"] is False


def test_real_rounds_shape_resolved_via_approved_next_round(tmp_path: Path) -> None:
    # Run not yet terminal, but a round AFTER the advice round approved (blank
    # critique) — the review passed on retry → resolved.
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(
        tmp_path, "d1.json", advice_relpath=relpath, feedback_source="ci_agent",
        phase="review_changes",
    )
    meta = _rounds_meta(
        [
            {"round": 1, "critique": "P1: broken"},
            {"round": 2, "critique": ""},   # blank critique ⟺ approved review
        ],
        status="running",
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["feedback_source"] == "ci_agent"
    assert call["outcome"] == "resolved"


def test_real_rounds_shape_resolved_via_final_acceptance(tmp_path: Path) -> None:
    # Run not terminal-done, no approved round persisted, but the structured
    # final_acceptance gate approved — it only runs after review approval.
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath,
                    phase="review_changes")
    meta = _rounds_meta(
        [{"round": 1, "critique": "P1: broken"}],
        status="running",
        extra={"final_acceptance": {"approved": True, "verdict": "APPROVED",
                                    "ship_ready": True, "findings": []}},
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    assert result["calls"][0]["outcome"] == "resolved"


def test_real_rounds_shape_repeated_via_later_advice(tmp_path: Path) -> None:
    # review_changes rejected → advice#1 retry → review rejected AGAIN → a second
    # advice fires for the same phase. The first retry did not end the loop, so
    # advice#1 is 'repeated' (never resolved). Run ends paused for an operator.
    rel1 = _write_advice(
        tmp_path, "h1.json", phase="review_changes", handoff_id="review_changes:repair_round:1",
        created_at="2026-06-13T10:00:00+00:00",
    )
    rel2 = _write_advice(
        tmp_path, "h2.json", phase="review_changes", handoff_id="review_changes:repair_round:2",
        created_at="2026-06-13T10:05:00+00:00",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=rel1, phase="review_changes",
                    handoff_id="review_changes:repair_round:1")
    _write_decision(tmp_path, "d2.json", advice_relpath=rel2, phase="review_changes",
                    handoff_id="review_changes:repair_round:2")
    meta = _rounds_meta(
        [
            {"round": 1, "critique": "P1: broken"},
            {"round": 2, "critique": "P1: still broken"},
        ],
        status="awaiting_phase_handoff",
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    first = result["calls"][0]
    assert first["handoff_id"] == "review_changes:repair_round:1"
    assert first["outcome"] == "repeated"
    assert first["repeated"] is True
    assert first["resolved"] is False
    assert result["summary"]["repeated"] >= 1
    # And the conservative rule never flips a re-rejected retry to resolved.
    assert result["summary"]["resolved_retries"] == 0


def test_real_rounds_shape_unknown_when_paused_without_signal(tmp_path: Path) -> None:
    # One review_changes advice retry, the run paused before any next verdict and
    # no downstream approval — genuinely unknown (never falsely resolved).
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath,
                    phase="review_changes")
    meta = _rounds_meta(
        [{"round": 1, "critique": "P1: broken"}],
        status="awaiting_phase_handoff",
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["outcome"] == "unknown"
    assert call["resolved"] is None
    # Advice fires only on a rejected/incomplete handoff — the trigger verdict is
    # surfaced even though the round shape carries no structured verdict.
    assert call["verdict"] == "REJECTED"
    assert call["trigger"] == "rejected"


def test_real_rounds_shape_repeated_uses_active_handoff_fingerprint(
    tmp_path: Path,
) -> None:
    # When the run is still paused, the active meta['phase_handoff'] payload
    # carries the rejected verdict + findings of the handoff that fired the
    # advice — surfaced as the call's finding_fingerprint without fabrication.
    rel1 = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
        created_at="2026-06-13T10:00:00+00:00",
    )
    rel2 = _write_advice(
        tmp_path, "h2.json", phase="review_changes",
        handoff_id="review_changes:repair_round:2",
        created_at="2026-06-13T10:05:00+00:00",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=rel1, phase="review_changes",
                    handoff_id="review_changes:repair_round:1")
    _write_decision(tmp_path, "d2.json", advice_relpath=rel2, phase="review_changes",
                    handoff_id="review_changes:repair_round:2")
    meta = _rounds_meta(
        [{"round": 1, "critique": "P1"}, {"round": 2, "critique": "P1"}],
        status="awaiting_phase_handoff",
    )
    # Active handoff for the SECOND (still-paused) advice carries the finding.
    meta["phase_handoff"] = {
        "id": "review_changes:repair_round:2",
        "phase": "review_changes",
        "verdict": "REJECTED",
        "artifacts": {"findings": [_finding("F1", "P1", "still broken")]},
    }
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    by_id = {c["handoff_id"]: c for c in result["calls"]}
    second = by_id["review_changes:repair_round:2"]
    assert second["verdict"] == "REJECTED"
    assert "F1|P1|still broken" in second["finding_fingerprint"]
    assert second.get("severity_counts") == {"P1": 1}


# ── terminal `done` must NOT falsely resolve a re-rejected retry (F1) ────────


def _write_plain_decision(
    run_dir: Path,
    name: str,
    *,
    action: str,
    phase: str = "review_changes",
    handoff_id: str,
    note: str | None = None,
) -> None:
    """A NON-advice (manual/operator) decision — note carries NO advice
    provenance, so the classifier must treat it as an override, not as the
    advice resolving the finding."""
    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": "r1",
        "handoff_id": handoff_id,
        "phase": phase,
        "action": action,
        "feedback": "operator override",
        "note": note,
        "decided_at": "2026-06-13T10:30:00+00:00",
    }
    decisions_dir.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")


def test_re_rejected_round_then_waiver_done_is_not_resolved(tmp_path: Path) -> None:
    # F1 repro: advice retry for review_changes:repair_round:1, then the next
    # review (round 2) re-rejects the SAME P1, then the operator continues with a
    # plain (non-advice) waiver and the run reaches terminal `done`. The advice
    # retry did NOT resolve the finding — must classify as 'repeated', never
    # 'resolved', despite status=done.
    rel = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=rel, phase="review_changes",
                    handoff_id="review_changes:repair_round:1")
    # Operator waives the SECOND, still-rejected handoff (no advice provenance).
    _write_plain_decision(
        tmp_path, "d2.json", action="continue_with_waiver",
        phase="review_changes", handoff_id="review_changes:repair_round:2",
    )
    meta = _rounds_meta(
        [
            {"round": 1, "critique": "P1: broken — fix the null check"},
            {"round": 2, "critique": "P1: still broken"},  # re-rejected
        ],
        status="done",
        extra={"final_acceptance": {"approved": True, "verdict": "APPROVED",
                                    "ship_ready": True, "findings": []}},
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["outcome"] == "repeated"
    assert call["resolved"] is False
    assert result["summary"]["resolved_retries"] == 0


def test_waiver_done_without_post_round_is_unknown_not_resolved(
    tmp_path: Path,
) -> None:
    # No round persisted after the advice round, but a non-advice waiver for the
    # same phase let the run reach `done` — the weak terminal/final_acceptance
    # signal must NOT read as the advice resolving it. Conservative: unknown.
    rel = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=rel, phase="review_changes",
                    handoff_id="review_changes:repair_round:1")
    meta = _rounds_meta(
        [{"round": 1, "critique": "P1: broken"}],
        status="done",
    )
    # Operator waiver recorded in meta (durable), phase matches the advice.
    meta["phase_handoff_waiver"] = {
        "handoff_id": "review_changes:repair_round:1",
        "phase": "review_changes",
        "waiver_text": "accepted risk",
        "findings": [_finding("F1", "P1", "broken")],
    }
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["outcome"] == "unknown"
    assert call["resolved"] is None


def test_later_approved_round_does_not_hide_nearest_rejection(
    tmp_path: Path,
) -> None:
    # F1: the advice retry's outcome is decided by the IMMEDIATELY following
    # review round, not by any later one. round 1 = advice retry, round 2 still
    # rejected (non-empty critique), round 3 approved, run done. The first advice
    # retry did NOT clear it — must be 'repeated', never 'resolved', even though
    # a later round passed and the run reached terminal done.
    rel = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=rel, phase="review_changes",
                    handoff_id="review_changes:repair_round:1")
    meta = _rounds_meta(
        [
            {"round": 1, "critique": "P1: broken"},
            {"round": 2, "critique": "P1: still broken"},  # nearest post-advice: reject
            {"round": 3, "critique": ""},                   # later approval
        ],
        status="done",
        extra={"final_acceptance": {"approved": True, "verdict": "APPROVED",
                                    "ship_ready": True, "findings": []}},
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    call = result["calls"][0]
    assert call["outcome"] == "repeated"
    assert call["resolved"] is False
    assert result["summary"]["resolved_retries"] == 0


def test_terminal_done_without_override_still_resolves(tmp_path: Path) -> None:
    # Guard against over-correction: a clean advice retry with NO re-rejected
    # round and NO non-advice override that reaches `done` stays 'resolved'.
    rel = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        handoff_id="review_changes:repair_round:1",
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=rel, phase="review_changes",
                    handoff_id="review_changes:repair_round:1")
    meta = _rounds_meta(
        [{"round": 1, "critique": "P1: broken"}],
        status="done",
    )
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    assert result["calls"][0]["outcome"] == "resolved"


# ── non-retry advice stops the loop ─────────────────────────────────────────


def test_halt_recommendation_is_stopped(tmp_path: Path) -> None:
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes", recommended_action="halt",
    )
    _write_decision(tmp_path, "d1.json", action="halt", advice_relpath=relpath)
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    assert result["calls"][0]["outcome"] == "stopped"


# ── usage / cost (no invented cost) ─────────────────────────────────────────


def test_cost_absent_when_usage_has_no_accounting(tmp_path: Path) -> None:
    _write_advice(
        tmp_path, "h1.json",
        usage={"tokens_in": 100, "tokens_out": 50, "model": "claude"},
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    call = result["calls"][0]
    assert call["tokens_in"] == 100
    assert call["tokens_out"] == 50
    assert call["model"] == "claude"
    assert "cost_usd_equivalent" not in call
    # Summary usage present but cost omitted (no accounting).
    assert result["summary"]["usage"]["tokens_in"] == 100
    assert "cost_usd_equivalent" not in result["summary"]["usage"]


def test_cost_aggregated_when_all_calls_have_accounting(tmp_path: Path) -> None:
    _write_advice(
        tmp_path, "h1.json", handoff_id="h1", created_at="2026-06-13T10:00:00+00:00",
        usage={"tokens_in": 10, "tokens_out": 5, "cost_usd_equivalent": 0.01},
    )
    _write_advice(
        tmp_path, "h2.json", handoff_id="h2", created_at="2026-06-13T10:01:00+00:00",
        usage={"tokens_in": 20, "tokens_out": 7, "cost_usd_equivalent": 0.02},
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    usage = result["summary"]["usage"]
    assert usage["tokens_in"] == 30
    assert usage["tokens_out"] == 12
    assert abs(usage["cost_usd_equivalent"] - 0.03) < 1e-9


def test_partial_accounting_omits_aggregate_cost(tmp_path: Path) -> None:
    """One call with cost, one without → aggregate cost omitted (not misleading)."""
    _write_advice(
        tmp_path, "h1.json", handoff_id="h1", created_at="2026-06-13T10:00:00+00:00",
        usage={"tokens_in": 10, "tokens_out": 5, "cost_usd_equivalent": 0.01},
    )
    _write_advice(
        tmp_path, "h2.json", handoff_id="h2", created_at="2026-06-13T10:01:00+00:00",
        usage={"tokens_in": 20, "tokens_out": 7},
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    assert "cost_usd_equivalent" not in result["summary"]["usage"]


def test_summary_usage_aggregates_cached_tokens_and_duration(tmp_path: Path) -> None:
    # F2: cache-read tokens and duration are kept per-call AND aggregated into
    # summary.usage (the upper layer writes metrics.json['handoff_advice'] from
    # this block, so dropping them here loses them from metrics).
    _write_advice(
        tmp_path, "h1.json", handoff_id="h1", created_at="2026-06-13T10:00:00+00:00",
        usage={"tokens_in": 100, "tokens_out": 50,
               "tokens_in_cache_read": 3, "duration_s": 4.5},
    )
    _write_advice(
        tmp_path, "h2.json", handoff_id="h2", created_at="2026-06-13T10:01:00+00:00",
        usage={"tokens_in": 20, "tokens_out": 7,
               "tokens_in_cache_read": 2, "duration_s": 1.0},
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    # Per-call fields preserved.
    assert result["calls"][0]["tokens_cached"] == 3
    assert result["calls"][0]["duration_s"] == 4.5
    # Aggregated into summary.usage (what feeds metrics.json['handoff_advice']).
    usage = result["summary"]["usage"]
    assert usage["tokens_cached"] == 5
    assert usage["duration_s"] == 5.5


def test_summary_usage_omits_cached_duration_when_absent(tmp_path: Path) -> None:
    # No cache/duration in the artifact → never fabricated as 0 in summary.
    _write_advice(
        tmp_path, "h1.json", usage={"tokens_in": 10, "tokens_out": 5},
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    usage = result["summary"]["usage"]
    assert "tokens_cached" not in usage
    assert "duration_s" not in usage


# ── decision-only surface (advice dir pruned) ───────────────────────────────


def test_decision_provenance_without_advice_dir_is_surface(tmp_path: Path) -> None:
    """A decision carrying advice provenance keeps the surface present (no None),
    even though no advice artifacts remain to build calls from."""
    _write_decision(
        tmp_path, "d1.json",
        advice_relpath="phase_handoff_advice/h1.json",
        feedback_source="ci_agent",
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    assert result["calls"] == []
    assert result["summary"]["calls"] == 0


# ── final_acceptance singleton-dict shape is adapted ────────────────────────


def test_final_acceptance_singleton_dict_resolves(tmp_path: Path) -> None:
    """meta['phases']['final_acceptance'] is a singleton dict (ADR 0025); the
    classifier must read it, not falsely degrade the outcome to unknown."""
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
    meta = _meta({
        "review_changes": [
            {"attempt": 1, "approved": False, "findings": [_finding("F1", "P1", "x")]},
            {"attempt": 2, "approved": True, "findings": []},
        ],
        "final_acceptance": {"approved": True, "verdict": "APPROVED", "findings": []},
    })
    result = collect_handoff_advice(tmp_path, meta)

    assert result is not None
    assert result["calls"][0]["outcome"] == "resolved"


# ── attempt-suffixed divergent advice are distinct calls ────────────────────


def test_attempt_suffixed_advice_are_distinct_calls(tmp_path: Path) -> None:
    _write_advice(
        tmp_path, "h1.json", handoff_id="h1",
        created_at="2026-06-13T10:00:00+00:00",
    )
    _write_advice(
        tmp_path, "h1_2.json", handoff_id="h1",
        created_at="2026-06-13T10:02:00+00:00",
    )
    result = collect_handoff_advice(tmp_path, _meta({}))

    assert result is not None
    assert len(result["calls"]) == 2
    assert result["calls"][0]["advice_artifact"] == "phase_handoff_advice/h1.json"
    assert result["calls"][1]["advice_artifact"] == "phase_handoff_advice/h1_2.json"
