"""Read-only handoff advisor module (T1).

Covers the parser (valid / fenced / garbage / unknown enums / empty
retry_feedback downgrade), the safety classifier (low-confidence, non-retry,
P1/P2 + unknown severity, waiver-never-auto), the durable advice artifact
(write/read, decisions-dir untouched, divergent → suffixed new file returning
the actual path, idempotent same-content), the eligibility predicate (including
the ``trigger rejected/incomplete but verdict approved → False`` case), and the
MockAgentProvider invocation path (no real providers).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.runtimes._strategy import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.project.handoff_advice import (
    AdviceContext,
    HandoffAdvice,
    advice_actions_available,
    build_advice_context,
    build_advice_prompt,
    build_provenance_note,
    classify_advice_safety,
    invoke_advisor,
    load_advice_artifact,
    parse_advice,
    write_advice_artifact,
)
from pipeline.runtime import PipelineState
from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffType
from sdk.phase_handoff import safe_handoff_id

_VALID_JSON = (
    '{"recommended_action": "retry_feedback", "confidence": "high", '
    '"rationale": "bounded fix", "retry_feedback": "fix the gap", '
    '"risks": ["scope creep"], "expected_files": ["a.py"], '
    '"operator_note": "ok"}'
)


def _signal(
    *,
    trigger: str = "rejected",
    verdict: str = "REJECTED",
    approved: bool = False,
    phase: str = "review_changes",
    available_actions: tuple[str, ...] = (
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ),
    artifacts: dict | None = None,
    last_output: str = "reviewer rejected the change",
) -> PhaseHandoffRequested:
    return PhaseHandoffRequested(
        handoff_id="review_changes:review:2",
        phase=phase,
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger=trigger,
        verdict=verdict,
        approved=approved,
        round_extras_key="review",
        round=2,
        loop_max_rounds=2,
        available_actions=available_actions,
        artifacts=artifacts or {},
        last_output=last_output,
    )


def _ctx(**kw) -> AdviceContext:
    base = dict(
        run_id="20260613_010101",
        handoff_id="review_changes:review:2",
        phase="review_changes",
        trigger="rejected",
        verdict="REJECTED",
        available_actions=("continue", "retry_feedback", "halt"),
    )
    base.update(kw)
    return AdviceContext(**base)


# ── parser ─────────────────────────────────────────────────────────────


def test_parse_valid_json() -> None:
    advice = parse_advice(_VALID_JSON)
    assert advice.recommended_action == "retry_feedback"
    assert advice.confidence == "high"
    assert advice.retry_feedback == "fix the gap"
    assert advice.risks == ("scope creep",)
    assert advice.expected_files == ("a.py",)
    assert advice.parse_warnings == ()


def test_parse_code_fenced_json() -> None:
    advice = parse_advice(f"```json\n{_VALID_JSON}\n```")
    assert advice.recommended_action == "retry_feedback"
    assert advice.retry_feedback == "fix the gap"


def test_parse_garbage_normalises_to_halt_low() -> None:
    advice = parse_advice("not json at all, sorry")
    assert advice.recommended_action == "halt"
    assert advice.confidence == "low"
    assert "advice_unparseable" in advice.parse_warnings


def test_parse_unknown_enums_normalise_safe() -> None:
    advice = parse_advice(
        '{"recommended_action": "frobnicate", "confidence": "certain", '
        '"rationale": "x", "retry_feedback": ""}'
    )
    assert advice.recommended_action == "halt"
    assert advice.confidence == "low"
    assert any("frobnicate" in w for w in advice.parse_warnings)
    assert any("certain" in w for w in advice.parse_warnings)


def test_parse_retry_without_feedback_downgrades() -> None:
    advice = parse_advice(
        '{"recommended_action": "retry_feedback", "confidence": "high", '
        '"rationale": "x", "retry_feedback": "   "}'
    )
    assert advice.recommended_action == "halt"
    assert any("no feedback" in w for w in advice.parse_warnings)


# ── safety classifier ──────────────────────────────────────────────────


def test_safety_high_retry_is_auto_appliable() -> None:
    advice = parse_advice(_VALID_JSON)
    safety = classify_advice_safety(advice, findings=[{"severity": "P3"}])
    assert safety.auto_apply_ok is True
    assert safety.needs_confirmation is False


def test_safety_low_confidence_needs_confirmation() -> None:
    advice = HandoffAdvice(
        recommended_action="retry_feedback", confidence="low",
        rationale="x", retry_feedback="fix it",
    )
    safety = classify_advice_safety(advice)
    assert safety.auto_apply_ok is False
    assert safety.needs_confirmation is True
    assert "confirmation" in safety.blocked_reason


def test_safety_non_retry_is_blocked() -> None:
    for action in ("continue", "halt"):
        advice = HandoffAdvice(
            recommended_action=action, confidence="high",
            rationale="x", retry_feedback="",
        )
        safety = classify_advice_safety(advice)
        assert safety.auto_apply_ok is False
        assert safety.blocked_reason


def test_safety_waiver_never_auto_applied() -> None:
    advice = HandoffAdvice(
        recommended_action="continue_with_waiver", confidence="high",
        rationale="x", retry_feedback="",
    )
    safety = classify_advice_safety(advice, findings=[{"severity": "P1"}])
    assert safety.auto_apply_ok is False
    assert safety.waiver_blocked is True


def test_safety_blocking_severity_flagged_for_p1_p2_and_unknown() -> None:
    advice = HandoffAdvice(
        recommended_action="continue", confidence="high",
        rationale="x", retry_feedback="",
    )
    for sev in ("P1", "P2", "", "totally-unknown"):
        safety = classify_advice_safety(advice, findings=[{"severity": sev}])
        assert safety.waiver_blocked is True, sev
    # A purely non-blocking finding set is not flagged.
    safety = classify_advice_safety(advice, findings=[{"severity": "P3"}])
    assert safety.waiver_blocked is False


# ── eligibility ────────────────────────────────────────────────────────


def test_eligible_rejected_with_findings() -> None:
    sig = _signal(artifacts={"findings": [{"id": "F1", "severity": "P2"}]})
    assert advice_actions_available(sig) is True


def test_eligible_incomplete_implement() -> None:
    sig = _signal(
        trigger="incomplete", verdict="INCOMPLETE", phase="implement",
        last_output="2 subtasks incomplete",
    )
    assert advice_actions_available(sig) is True


def test_not_eligible_trigger_rejected_but_verdict_approved() -> None:
    # human_feedback_always can fire 'rejected' trigger machinery, but an
    # approved verdict (approved=True / APPROVED) is NOT advisory-eligible.
    sig = _signal(trigger="rejected", verdict="APPROVED", approved=True)
    assert advice_actions_available(sig) is False


def test_not_eligible_approved_verdict_label_even_if_approved_false() -> None:
    sig = _signal(trigger="rejected", verdict="APPROVED", approved=False)
    assert advice_actions_available(sig) is False


def test_not_eligible_when_retry_feedback_absent() -> None:
    sig = _signal(
        available_actions=("continue", "halt"),
        artifacts={"findings": [{"id": "F1"}]},
    )
    assert advice_actions_available(sig) is False


def test_not_eligible_without_output_or_findings() -> None:
    sig = _signal(last_output="", artifacts={})
    assert advice_actions_available(sig) is False


def test_not_eligible_unrelated_trigger() -> None:
    sig = _signal(trigger="approved", verdict="APPROVED", approved=True)
    assert advice_actions_available(sig) is False


# ── context assembly ───────────────────────────────────────────────────


def test_build_context_truncates_and_pulls_findings(tmp_path: Path) -> None:
    big = "x" * 9000
    sig = _signal(
        artifacts={
            "findings": [
                {"id": "F1", "severity": "P2", "title": "gap",
                 "required_fix": "add test", "body": "y" * 9000},
            ],
            "short_summary": "rejected: missing coverage",
        },
        last_output=big,
    )
    run = SimpleNamespace(
        state=SimpleNamespace(task="Fix the bug\nmore detail"),
        git_cwd="",  # empty → diff summary best-effort empty
        session_ts="20260613_010101",
    )
    ctx = build_advice_context(run, sig)
    assert ctx.run_id == "20260613_010101"
    assert ctx.task_title == "Fix the bug"
    assert len(ctx.last_output) < len(big)
    assert ctx.findings[0]["required_fix"] == "add test"
    assert "[truncated]" in ctx.findings[0]["body"]
    assert ctx.last_phase_summary == "rejected: missing coverage"
    # The composed prompt carries the marker the mock keys on.
    assert "[handoff_advice]" in build_advice_prompt(ctx)


def test_build_context_infers_russian_response_language(tmp_path: Path) -> None:
    sig = _signal(
        artifacts={
            "findings": [
                {
                    "id": "F1",
                    "severity": "P2",
                    "title": "Не определена классификация destructive_action",
                    "required_fix": "Добавить явное правило классификации.",
                },
            ],
            "short_summary": "План исправил scope, но safety gate не определен.",
        },
        last_output="Вердикт: REJECTED. Нужно исправить только F1.",
    )
    run = SimpleNamespace(
        state=SimpleNamespace(task="Исправить handoff advice"),
        git_cwd="",
        session_ts="20260613_010101",
    )

    ctx = build_advice_context(run, sig)
    prompt = build_advice_prompt(ctx)

    assert ctx.response_language == "Russian"
    assert "Write human-readable JSON string values" in prompt
    assert "in Russian" in prompt
    assert "JSON keys, protocol enum values" in prompt


# ── durable artifact ───────────────────────────────────────────────────


def test_write_returns_relpath_and_reads_back(tmp_path: Path) -> None:
    advice = parse_advice(_VALID_JSON)
    ctx = _ctx()
    rel = write_advice_artifact(
        tmp_path, ctx.handoff_id, advice, ctx, created_at="2026-06-13T00:00:00+00:00",
    )
    safe = safe_handoff_id(ctx.handoff_id)
    assert rel == f"phase_handoff_advice/{safe}.json"
    loaded = load_advice_artifact(tmp_path / rel)
    assert loaded is not None
    assert loaded["advice"]["recommended_action"] == "retry_feedback"
    assert loaded["run_id"] == ctx.run_id
    assert loaded["response_language"] == ctx.response_language
    # The decisions directory must never be created by the advice writer.
    assert not (tmp_path / "phase_handoff_decisions").exists()


def test_write_is_idempotent_for_identical_advice(tmp_path: Path) -> None:
    advice = parse_advice(_VALID_JSON)
    ctx = _ctx()
    rel1 = write_advice_artifact(tmp_path, ctx.handoff_id, advice, ctx)
    rel2 = write_advice_artifact(tmp_path, ctx.handoff_id, advice, ctx)
    assert rel1 == rel2
    # Only the single base file exists.
    safe = safe_handoff_id(ctx.handoff_id)
    files = sorted((tmp_path / "phase_handoff_advice").glob("*.json"))
    assert [f.name for f in files] == [f"{safe}.json"]


def test_divergent_advice_writes_new_suffixed_file(tmp_path: Path) -> None:
    ctx = _ctx()
    first = parse_advice(_VALID_JSON)
    rel1 = write_advice_artifact(tmp_path, ctx.handoff_id, first, ctx)
    second = HandoffAdvice(
        recommended_action="halt", confidence="medium",
        rationale="no safe path", retry_feedback="",
    )
    rel2 = write_advice_artifact(tmp_path, ctx.handoff_id, second, ctx)
    safe = safe_handoff_id(ctx.handoff_id)
    assert rel1 == f"phase_handoff_advice/{safe}.json"
    assert rel2 == f"phase_handoff_advice/{safe}_2.json"
    # The original artifact is intact (never overwritten).
    loaded1 = load_advice_artifact(tmp_path / rel1)
    assert loaded1["advice"]["recommended_action"] == "retry_feedback"
    loaded2 = load_advice_artifact(tmp_path / rel2)
    assert loaded2["advice"]["recommended_action"] == "halt"


def test_provenance_note_uses_actual_path() -> None:
    note = build_provenance_note("phase_handoff_advice/foo_2.json")
    assert note == (
        "feedback_source=agent_advice; "
        "advice_artifact=phase_handoff_advice/foo_2.json"
    )


# ── mock invocation (no real providers) ────────────────────────────────


def _run(tmp_path: Path, agent) -> SimpleNamespace:
    state = PipelineState(
        task="resolve the rejected change",
        project_dir=str(tmp_path),
        plugin=PluginConfig(),
    )
    state.phase_config = SimpleNamespace(review_changes_agent=agent)
    return SimpleNamespace(
        state=state, git_cwd=str(tmp_path), session_ts="20260613_010101",
    )


def test_invoke_advisor_via_mock_provider(tmp_path: Path) -> None:
    agent = MockAgentProvider().claude("mock")
    run = _run(tmp_path, agent)
    ctx = _ctx(findings=({"id": "F1", "severity": "P2"},))
    result = invoke_advisor(run, ctx)
    assert "[handoff_advice]" in agent.last_prompt
    assert result.advice.recommended_action == "retry_feedback"
    assert result.advice.confidence == "high"
    assert result.advice.retry_feedback
    # Usage was captured under the dedicated handoff_advice extras slot.
    assert "_phase_handoff_advice_usage" in run.state.extras


def test_invoke_advisor_injected_agent_overrides_config(tmp_path: Path) -> None:
    config_agent = MockAgentProvider().claude("mock")
    injected = MockAgentProvider().codex("mock")
    run = _run(tmp_path, config_agent)
    result = invoke_advisor(run, _ctx(), agent=injected)
    assert "[handoff_advice]" in injected.last_prompt
    assert config_agent.last_prompt == ""
    assert result.advice.recommended_action == "retry_feedback"


def test_invoke_advisor_usage_not_double_counted_into_totals(
    tmp_path: Path,
) -> None:
    # T4 (ADR 0093): the advisor runs OUTSIDE the FSM phase loop, so recording
    # its usage as a ``handoff_advice`` MetricsCollector phase double-counted it
    # into metrics.json ``total_*``. Usage is now captured ONLY in the in-memory
    # extras aggregate here; the durable, observe-only metrics surfacing is the
    # upper layer's job (re-derived from durable artifacts — see
    # tests/unit/pipeline/project/test_handoff_advice_metrics_integration.py).
    import json

    from core.observability.metrics import MetricsCollector

    agent = MockAgentProvider().claude("mock")
    run = _run(tmp_path, agent)
    metrics = MetricsCollector(default_model="mock")
    run._metrics = metrics
    invoke_advisor(run, _ctx(findings=({"id": "F1", "severity": "P2"},)))

    # Usage captured in the in-memory aggregate (mock provider produced tokens)…
    assert "_phase_handoff_advice_usage" in run.state.extras
    # … but NEVER folded into the metrics totals or phases (no double count).
    out_path = metrics.save(tmp_path)
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))
    assert "handoff_advice" not in data.get("phases", {})
    assert data["total_tokens"] == 0
    assert data["total_tokens_in"] == 0
    assert data["total_tokens_out"] == 0
