# SPDX-License-Identifier: Apache-2.0
"""Unified DONE/HALTED 'Agent advice:' block (T3-summary).

These cover ``pipeline.project.finalization._render_agent_advice_summary`` and
its formatter ``_format_agent_advice_block``: the block is driven by the durable
advice digest (``collect_handoff_advice`` over the run dir, fed ``run.session``
== the meta-form mapping with ``'phases'``) so its counts match the evidence
section, covers both the human-driven (``agent_advice``) and CI-policy
(``ci_agent``) sources, renders ONLY when advice evidence exists, and never
invents or double-counts cost. The no-run_dir fallback to the in-memory
``_ci_agent_advice`` aggregate is exercised too.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline.project.finalization import (
    FinalizationContext,
    _render_agent_advice_summary,
    _render_ci_agent_advice_summary,
    _render_evidence_summary,
    _scope_expansion_summary_lines,
    build_companion_delivery_caveat,
    finalize_project_run,
)
from pipeline.project.terminal_delivery import render_delivery_destination_lines

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
    advice_dir.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")
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


def _session_resolved() -> dict[str, Any]:
    """Meta-form session: review_changes rejected then approved after retry."""
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


# ── delivery destination line ───────────────────────────────────────────────


def test_delivery_line_published_branch_shows_push_and_pr() -> None:
    session = {
        "commit_delivery": {
            "status": "committed",
            "delivery_branch": "orcho/deliver/r1-feature",
            "pr_url": "https://example.test/pr/7",
        },
    }
    assert render_delivery_destination_lines(session) == (
        "Delivery: pushed orcho/deliver/r1-feature → PR https://example.test/pr/7",
    )


def test_delivery_line_published_branch_without_pr_is_actionable() -> None:
    session = {
        "commit_delivery": {
            "status": "committed",
            "delivery_branch": "orcho/deliver/r1-feature",
            "pr_url": None,
        },
    }
    assert render_delivery_destination_lines(session) == (
        "Delivery: pushed orcho/deliver/r1-feature → "
        "branch ready — open a PR / push manually",
    )


def test_delivery_line_checkout_commit_shows_sha7() -> None:
    session = {
        "commit_delivery": {
            "status": "committed",
            "commit_sha": "0123456789abcdef",
            "pr_url": None,
        },
    }
    assert render_delivery_destination_lines(session) == (
        "Delivery: committed 0123456 to project checkout",
    )


def test_delivery_line_applied_uncommitted() -> None:
    session = {"commit_delivery": {"status": "applied_uncommitted", "pr_url": None}}
    assert render_delivery_destination_lines(session) == (
        "Delivery: applied to project checkout (uncommitted)",
    )


def test_delivery_line_skipped() -> None:
    session = {"commit_delivery": {"status": "skipped", "pr_url": None}}
    assert render_delivery_destination_lines(session) == (
        "Delivery: skipped — diff retained",
    )


def test_delivery_line_halted_reports_not_delivered() -> None:
    session = {"commit_delivery": {"status": "halted", "pr_url": None}}
    assert render_delivery_destination_lines(session) == (
        "Delivery: not delivered (halted)",
    )


def test_delivery_line_absent_or_pending_renders_nothing() -> None:
    assert render_delivery_destination_lines({}) == ()
    assert render_delivery_destination_lines(
        {"commit_delivery": {"status": "pending", "pr_url": None}}
    ) == ()
    assert render_delivery_destination_lines({"commit_delivery": None}) == ()


# ── omit when no advice ─────────────────────────────────────────────────────


def test_block_omitted_without_advice_surface(tmp_path: Path) -> None:
    """A run with no Stage 0/1 advice surface renders nothing (None)."""
    assert _render_agent_advice_summary(
        tmp_path, {"status": "done", "phases": {}}, {}, include_accounting=False,
    ) is None


# ── interactive (agent_advice) source ───────────────────────────────────────


def test_block_renders_for_agent_advice_resolved(tmp_path: Path) -> None:
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(
        tmp_path, "d1.json", advice_relpath=relpath, feedback_source="agent_advice",
    )
    block = _render_agent_advice_summary(
        tmp_path, _session_resolved(), {}, include_accounting=False,
    )
    assert block is not None
    assert "Agent advice:" in block
    assert "calls=1 applied_retries=1 resolved=1 repeated=0 stopped=0" in block
    assert "by source: agent_advice=1" in block


# ── CI (ci_agent) source ────────────────────────────────────────────────────


def test_block_renders_for_ci_agent_source(tmp_path: Path) -> None:
    relpath = _write_advice(tmp_path, "h1.json", phase="review_changes")
    _write_decision(
        tmp_path, "d1.json", advice_relpath=relpath, feedback_source="ci_agent",
    )
    block = _render_agent_advice_summary(
        tmp_path, _session_resolved(), {}, include_accounting=False,
    )
    assert block is not None
    assert "Agent advice:" in block
    assert "by source: ci_agent=1" in block
    assert "resolved=1" in block


# ── usage line: tokens always, cost only with accounting ────────────────────


def test_usage_line_shows_cost_only_with_accounting(tmp_path: Path) -> None:
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        usage={"tokens_in": 100, "tokens_out": 50, "cost_usd_equivalent": 0.25},
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
    session = _session_resolved()

    # Accounting off: tokens shown, cost suppressed (never invented / leaked).
    off = _render_agent_advice_summary(
        tmp_path, session, {}, include_accounting=False,
    )
    assert "usage: tokens=150" in off
    assert "cost_ref" not in off

    # Accounting on: cost_ref appended from the digest.
    on = _render_agent_advice_summary(
        tmp_path, session, {}, include_accounting=True,
    )
    assert "usage: tokens=150 cost_ref=runtime-reported:$0.25" in on


def test_usage_cost_omitted_when_digest_lacks_accounting(tmp_path: Path) -> None:
    """Accounting enabled but the advice usage had no cost → tokens only."""
    relpath = _write_advice(
        tmp_path, "h1.json", phase="review_changes",
        usage={"tokens_in": 100, "tokens_out": 50},
    )
    _write_decision(tmp_path, "d1.json", advice_relpath=relpath)
    block = _render_agent_advice_summary(
        tmp_path, _session_resolved(), {}, include_accounting=True,
    )
    assert "usage: tokens=150" in block
    assert "cost_ref" not in block


# ── no run_dir → fallback to the in-memory CI aggregate ─────────────────────


def test_fallback_to_ci_aggregate_when_no_run_dir() -> None:
    extras = {
        "_ci_agent_advice": {
            "retries": 2, "resolved": 1, "stopped": 1,
            "last_recommendation": "retry_feedback", "last_confidence": "high",
        },
    }
    block = _render_agent_advice_summary(
        None, {"status": "done"}, extras, include_accounting=False,
    )
    assert block is not None
    assert "Agent advice:" in block
    assert "ci_agent retries=2 resolved=1 stopped=1" in block
    # Same as the standalone fallback renderer.
    assert block == _render_ci_agent_advice_summary(extras)


def test_no_run_dir_no_aggregate_renders_nothing() -> None:
    assert _render_agent_advice_summary(
        None, {"status": "done"}, {}, include_accounting=False,
    ) is None


# ── call-site gate: advice cost is accounting-driven, NOT phase-cost-driven ──
#
# Blocker-2 regression: the advice block's cost line was gated on
# ``has_api_equivalent_cost`` (PHASE-cost presence) at the
# ``_render_agent_advice_summary`` call in ``finalize_project_run``. An advice
# digest can carry a real ``usage.cost_usd_equivalent`` while NO phase reported
# cost reference (e.g. an operator/CI stop with no following phase), so the
# advice cost was wrongly suppressed. These integration cases drive the real
# call site to prove the fix: cost renders iff accounting is enabled AND the
# digest carried a cost, independent of phase cost, while
# ``has_api_equivalent_cost`` (phase-cost driven) stays unchanged.


def _make_finalize_run(tmp_path: Path) -> SimpleNamespace:
    """Minimal ``finalize_project_run``-shaped run with ZERO phase cost.

    ``_metrics.phases == []`` so the phase-cost-driven
    ``has_api_equivalent_cost`` is always False; only the advice digest can
    carry a cost here.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    state = SimpleNamespace(
        extras={}, phase_log={}, halt=False, halt_reason=None,
    )
    return SimpleNamespace(
        state=state,
        session=_session_resolved(),
        session_ts="20260613_1",
        output_dir=run_dir,
        git_cwd=str(run_dir),
        profile_name="default",
        parent_run_id=None,
        project_alias=None,
        project_path=project_dir,
        worktree_context=None,
        _done_summary_profile=None,
        _ckpt=None,
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        task="# Orcho Task: advice cost",
        _metrics=SimpleNamespace(
            save=lambda d: d / "metrics.json",
            summary_line=lambda: "Tokens: 0",
            as_dict=lambda: {},
            phases=[],
        ),
        _effective_diff_cwd=lambda: project_dir,
        _commit_delivery_baseline=lambda: "HEAD",
        _run_commit_delivery=lambda diff_cwd: None,
    )


def _wire_finalize_stubs(monkeypatch, *, accounting_enabled: bool) -> None:
    monkeypatch.setattr(
        "pipeline.engine.run_diff.capture_run_diff", lambda *a, **k: None,
    )

    def _save(output_dir, _session):
        p = output_dir / "session.json"
        p.write_text("{}", encoding="utf-8")
        return p

    monkeypatch.setattr("pipeline.project.finalization.save_session", _save)
    monkeypatch.setattr(
        "pipeline.evidence.write_bundle_or_placeholder",
        lambda output_dir, *, run_id, status: output_dir / "evidence.json",
    )
    monkeypatch.setattr(
        "pipeline.engine.artifact_mirror.mirror_to_projects", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "pipeline.observability.context_pressure.format_context_summary",
        lambda _session: None,
    )
    monkeypatch.setattr(
        "core.infra.config.AppConfig.load",
        lambda: SimpleNamespace(artifacts={}, commit={}, accounting={}),
    )
    # The advice block's cost gate reads accounting availability ALONE.
    monkeypatch.setattr(
        "core.infra.config.accounting_enabled", lambda: accounting_enabled,
    )


def test_advice_cost_renders_with_accounting_and_zero_phase_cost(
    tmp_path, monkeypatch,
) -> None:
    """Accounting enabled + advice digest cost + NO phase cost → cost_ref shows.

    ``has_api_equivalent_cost`` stays False (no phase reported cost), proving
    the advice cost line is gated on accounting availability, not phase cost.
    """
    relpath = _write_advice(
        tmp_path / "run", "h1.json", phase="review_changes",
        usage={"tokens_in": 100, "tokens_out": 50, "cost_usd_equivalent": 0.25},
    )
    _write_decision(tmp_path / "run", "d1.json", advice_relpath=relpath)
    run = _make_finalize_run(tmp_path)
    _wire_finalize_stubs(monkeypatch, accounting_enabled=True)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.has_api_equivalent_cost is False
    assert result.ci_agent_advice_summary is not None
    assert (
        "usage: tokens=150 cost_ref=runtime-reported:$0.25"
        in result.ci_agent_advice_summary
    )


def test_advice_cost_hidden_when_accounting_disabled(
    tmp_path, monkeypatch,
) -> None:
    """Accounting OFF → advice usage shows tokens only, never invents cost."""
    relpath = _write_advice(
        tmp_path / "run", "h1.json", phase="review_changes",
        usage={"tokens_in": 100, "tokens_out": 50, "cost_usd_equivalent": 0.25},
    )
    _write_decision(tmp_path / "run", "d1.json", advice_relpath=relpath)
    run = _make_finalize_run(tmp_path)
    _wire_finalize_stubs(monkeypatch, accounting_enabled=False)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.ci_agent_advice_summary is not None
    assert "usage: tokens=150" in result.ci_agent_advice_summary
    assert "cost_ref" not in result.ci_agent_advice_summary


def test_advice_cost_hidden_when_digest_has_no_cost(
    tmp_path, monkeypatch,
) -> None:
    """Accounting on but the advice digest carried no cost → tokens only."""
    relpath = _write_advice(
        tmp_path / "run", "h1.json", phase="review_changes",
        usage={"tokens_in": 100, "tokens_out": 50},
    )
    _write_decision(tmp_path / "run", "d1.json", advice_relpath=relpath)
    run = _make_finalize_run(tmp_path)
    _wire_finalize_stubs(monkeypatch, accounting_enabled=True)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.ci_agent_advice_summary is not None
    assert "usage: tokens=150" in result.ci_agent_advice_summary
    assert "cost_ref" not in result.ci_agent_advice_summary


def test_no_advice_evidence_renders_no_block_at_call_site(
    tmp_path, monkeypatch,
) -> None:
    """A run with no advice surface renders no 'Agent advice:' block, even with
    accounting enabled."""
    run = _make_finalize_run(tmp_path)
    _wire_finalize_stubs(monkeypatch, accounting_enabled=True)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.ci_agent_advice_summary is None


# ── T2: companion delivery caveat (primary committed + companion dirty) ──────


def _multi_block(*, primary_status: str, companions: list[dict]) -> dict:
    return {"primary_status": primary_status, "companions": companions}


def _dirty_companion() -> dict:
    return {
        "alias": "orcho-mcp",
        "path": "/ws/orcho-mcp",
        "state": "dirty",
        "changed_paths": ["[orcho-mcp]/server.py"],
    }


def test_caveat_built_for_committed_primary_with_dirty_companion() -> None:
    session = {
        "status": "done",
        "multi_project_delivery": _multi_block(
            primary_status="committed", companions=[_dirty_companion()],
        ),
    }
    caveat = build_companion_delivery_caveat(session)
    assert caveat is not None
    assert caveat.primary_status == "committed"
    assert [c["alias"] for c in caveat.dirty_companions] == ["orcho-mcp"]
    joined = "\n".join(caveat.lines)
    assert "Companion delivery incomplete" in joined
    assert "[orcho-mcp]/server.py" in joined
    # Both actionable next steps are offered.
    assert "review and commit" in joined
    assert "cross-run / follow-up" in joined


def test_caveat_built_for_applied_uncommitted_primary() -> None:
    session = {
        "multi_project_delivery": _multi_block(
            primary_status="applied_uncommitted", companions=[_dirty_companion()],
        ),
    }
    assert build_companion_delivery_caveat(session) is not None


def test_no_caveat_for_clean_single_repo_run() -> None:
    assert build_companion_delivery_caveat({"status": "done"}) is None


def test_no_caveat_when_all_companions_committed() -> None:
    session = {
        "multi_project_delivery": _multi_block(
            primary_status="committed",
            companions=[{
                "alias": "orcho-mcp", "path": "/ws/orcho-mcp",
                "state": "committed", "changed_paths": ["[orcho-mcp]/server.py"],
            }],
        ),
    }
    assert build_companion_delivery_caveat(session) is None


def test_no_caveat_when_primary_not_delivered() -> None:
    # A still-pending primary (e.g. parked delivery gate) is not a caveat: the
    # primary itself has not shipped yet, so completeness is decided elsewhere.
    session = {
        "multi_project_delivery": _multi_block(
            primary_status="pending", companions=[_dirty_companion()],
        ),
    }
    assert build_companion_delivery_caveat(session) is None


def test_finalize_surfaces_companion_caveat_after_delivery(
    tmp_path, monkeypatch,
) -> None:
    """Finalize order: delivery (step 2) records the block, caveat built after.

    The delivery step is what writes ``session['multi_project_delivery']`` (run.py
    propagation, mirrored here); the silent service must read it when building the
    result so a committed primary with a dirty companion never finalizes as a
    fully-complete run.
    """
    run = _make_finalize_run(tmp_path)
    run.session = {"status": "done", "phases": {}}

    def _delivery(diff_cwd) -> None:
        run.session["multi_project_delivery"] = _multi_block(
            primary_status="committed", companions=[_dirty_companion()],
        )

    run._run_commit_delivery = _delivery
    _wire_finalize_stubs(monkeypatch, accounting_enabled=False)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.status == "done"
    assert result.companion_caveat is not None
    assert [c["alias"] for c in result.companion_caveat.dirty_companions] == [
        "orcho-mcp",
    ]


def test_finalize_no_caveat_for_clean_run(tmp_path, monkeypatch) -> None:
    run = _make_finalize_run(tmp_path)
    run.session = {"status": "done", "phases": {}}
    run._run_commit_delivery = lambda diff_cwd: None
    _wire_finalize_stubs(monkeypatch, accounting_enabled=False)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.companion_caveat is None


# ── T2: run.py propagation of the T1 disclosure into the durable block ───────


def test_record_multi_project_delivery_writes_block_from_decision() -> None:
    from pipeline.engine.companion_scope import CompanionRepo, CompanionRepoState
    from pipeline.project.run import _record_multi_project_delivery

    session: dict[str, Any] = {}
    decision = SimpleNamespace(
        status="committed",
        scope_companions=(
            CompanionRepo(
                alias="orcho-mcp",
                path="/ws/orcho-mcp",
                state=CompanionRepoState.DIRTY,
                changed_paths=("[orcho-mcp]/server.py",),
            ),
        ),
    )

    _record_multi_project_delivery(session, decision)

    block = session["multi_project_delivery"]
    assert block["primary_status"] == "committed"
    assert block["companions"] == [{
        "alias": "orcho-mcp",
        "path": "/ws/orcho-mcp",
        "state": "dirty",
        "changed_paths": ["[orcho-mcp]/server.py"],
    }]


def test_record_multi_project_delivery_noop_without_companions() -> None:
    from pipeline.project.run import _record_multi_project_delivery

    session: dict[str, Any] = {}
    decision = SimpleNamespace(status="committed", scope_companions=())

    _record_multi_project_delivery(session, decision)

    assert "multi_project_delivery" not in session


# ── T4: dog-food regression at the finalization surface ──────────────────────
#
# Run 20260625_181331_2a392f: target orcho-core committed, mandatory companion
# orcho-mcp dirty at delivery. The finalization surface must carry a companion
# caveat (repo name + changed paths) and an actionable next step, never a
# fully-complete DONE. Fails on the pre-T2 baseline (no companion_caveat field).


def test_dogfood_finalize_caveat_committed_primary_dirty_companion(
    tmp_path, monkeypatch,
) -> None:
    run = _make_finalize_run(tmp_path)
    run.session = {"status": "done", "phases": {}}

    def _delivery(diff_cwd) -> None:
        run.session["multi_project_delivery"] = _multi_block(
            primary_status="committed",
            companions=[{
                "alias": "orcho-mcp",
                "path": "/ws/orcho-mcp",
                "state": "dirty",
                "changed_paths": ["[orcho-mcp]/server.py"],
            }],
        )

    run._run_commit_delivery = _delivery
    _wire_finalize_stubs(monkeypatch, accounting_enabled=False)

    result = finalize_project_run(FinalizationContext(run=run))

    # Primary delivery reports as before (run is DONE) ...
    assert result.status == "done"
    # ... but the run is NOT presented as fully complete: a companion caveat rides.
    caveat = result.companion_caveat
    assert caveat is not None
    assert caveat.primary_status == "committed"
    assert [c["alias"] for c in caveat.dirty_companions] == ["orcho-mcp"]
    joined = "\n".join(caveat.lines)
    assert "Companion delivery incomplete" in joined
    assert "orcho-mcp" in joined
    assert "[orcho-mcp]/server.py" in joined
    # Actionable next step: handle the companion delivery.
    assert "review and commit" in joined
    assert "cross-run / follow-up" in joined


def test_dogfood_finalize_clean_single_repo_control_no_caveat(
    tmp_path, monkeypatch,
) -> None:
    """Control: a clean single-repo run finalizes with no companion caveat."""
    run = _make_finalize_run(tmp_path)
    run.session = {"status": "done", "phases": {}}
    run._run_commit_delivery = lambda diff_cwd: None
    _wire_finalize_stubs(monkeypatch, accounting_enabled=False)

    result = finalize_project_run(FinalizationContext(run=run))

    assert result.companion_caveat is None


# ── F2: scope-expansion summary in the Evidence block ───────────────────────


def _scope_session(*signals) -> dict[str, Any]:
    """Build a session whose canonical final_acceptance path carries scope
    evidence (the projection T2's adapter writes from the durable phase_log)."""
    from pipeline.engine.scope_expansion import build_scope_expansion_assessment

    assessment = build_scope_expansion_assessment(list(signals))
    return {
        "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "scope_expansion": assessment.to_dict(),
            },
        },
    }


def _build_signal(**kw):
    from pipeline.engine.scope_expansion import FileScopeSignals

    return FileScopeSignals(**kw)


def test_evidence_summary_renders_scope_expansion_notice() -> None:
    session = _scope_session(
        _build_signal(
            path="package-lock.json", category="build",
            verified=True, has_explanation=True,
        ),
    )

    lines = _render_evidence_summary(session)
    joined = "\n".join(lines)

    assert "Scope expanded:" in joined
    assert "package-lock.json — build" in joined
    # Always-visible: a notice is surfaced even though it does not block.


def test_evidence_summary_renders_scope_expansion_blocker() -> None:
    session = _scope_session(
        _build_signal(
            path="storage/cache.py", category="persistence", is_persistence=True,
        ),
    )

    lines = _render_evidence_summary(session)
    joined = "\n".join(lines)

    assert "Scope expansion blocker:" in joined
    assert "storage/cache.py — persistence" in joined


def test_evidence_summary_omits_scope_block_without_evidence() -> None:
    # No scope_expansion key at the canonical path → nothing rendered.
    session = {
        "status": "done",
        "phases": {"final_acceptance": {"verdict": "APPROVED"}},
    }

    lines = _render_evidence_summary(session)

    assert not any("Scope" in line for line in lines)


def test_scope_expansion_summary_lines_reads_only_canonical_path() -> None:
    # A diverging key (not the canonical session path) is ignored.
    phases = {
        "final_acceptance": {
            "verdict": "APPROVED",
            "scope_expansion_other": {"items": [{"status": "x"}]},
        },
    }
    assert _scope_expansion_summary_lines(phases) == ()

    # Empty-items evidence renders nothing (byte-identical).
    phases_empty = {
        "final_acceptance": {"scope_expansion": {"items": [], "has_blocker": False}},
    }
    assert _scope_expansion_summary_lines(phases_empty) == ()
