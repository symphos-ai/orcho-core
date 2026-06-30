"""ADR 0038 — cross_plan phase handoff parity tests.

Exercises the cross-orchestrator's pause → operator decision →
resume lifecycle for cross_plan rejection budget exhaustion.
Mirrors the single-run scenarios B/C from the post-promote MCP
smoke (handoff payload shape, resume continue / retry_feedback /
halt action handling).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.pipeline.cross_project.test_cross_orchestrator import (
    _approved_review_json,
    _cp_json,
    _cross_test_appconfig_mock,
    _rejected_review_json,
    _ScriptedProvider,
)


def _make_projects(tmp_path: Path) -> dict[str, Path]:
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()
    return {"api": api, "web": web}


def _seed_decision(
    run_dir: Path,
    *,
    handoff_id: str,
    action: str,
    feedback: str | None = None,
    note: str | None = None,
) -> None:
    """Drop a decision artifact via the public SDK helper so the cross
    resume path reads it the same way ``orcho_phase_handoff_decide``
    would have written it.

    Skips the active-handoff validation by writing the file directly —
    decide() requires the run's meta.json to carry an active payload
    matching the handoff_id, but for resume-side tests we want to
    isolate the decision-read path from the pause-emission path."""
    from sdk.phase_handoff import safe_handoff_id

    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id":      run_dir.name,
        "handoff_id":  handoff_id,
        "phase":       "cross_plan",
        "action":      action,
        "feedback":    feedback,
        "note":        note,
        "decided_at":  "2026-05-24T12:00:00+00:00",
    }
    (decisions_dir / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_cross_checkpoint_with_handoff(
    run_dir: Path,
    *,
    handoff_id: str,
) -> None:
    ckpt = {
        "phase0_done":           False,
        "sub_status":            {},
        "phase_handoff_pending": True,
        "phase_handoff_id":      handoff_id,
    }
    (run_dir / "cross_checkpoint.json").write_text(
        json.dumps(ckpt, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _paused_meta(
    *, round_n: int, prior_rounds: list[dict] | None = None,
) -> dict:
    """Build the persisted ``meta.json`` shape for a paused cross run.

    Mirrors what ``_apply_cross_phase_handoff_pause`` lands on disk:
    ``meta.status="awaiting_phase_handoff"`` + ``meta.phase_handoff``
    payload + ``meta.phases.cross_plan.rounds`` audit trace. Used by
    multi-cycle retry tests so resume sees the same state the CLI
    would load via :func:`load_resume_meta`.
    """
    handoff_id = f"cross_plan:cross_plan_round:{round_n}"
    return {
        "status":         "awaiting_phase_handoff",
        "phase_handoff": {
            "id":                handoff_id,
            "phase":             "cross_plan",
            "type":              "human_feedback_on_reject",
            "trigger":           "rejected",
            "verdict":           "REJECTED",
            "approved":          False,
            "round_extras_key":  "cross_plan_round",
            "round":             round_n,
            "loop_max_rounds":   2,
            "available_actions": [
                "continue", "retry_feedback", "halt",
            ],
            "artifacts": {
                "short_summary": "rejected",
                "findings":      [],
                "risks":         [],
                "checks":        [],
            },
            "last_output": "stale plan",
        },
        "phases": {
            "cross_plan": {
                "output":   "stale plan",
                "run_dir":  "",
                "rounds":   prior_rounds or [],
                "approved": False,
            },
        },
    }


# ── Pause emission ───────────────────────────────────────────────────────────


def test_cross_plan_handoff_payload_shape_matches_single_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause payload must mirror single-run ``meta.phase_handoff``
    field-for-field so MCP / SDK / UI consumers that already render
    single-run handoffs handle cross handoffs without code changes."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
    )
    handoff = session["phase_handoff"]
    # Single-run shape (ADR 0031): id, phase, type, trigger, verdict,
    # approved, round_extras_key, round, loop_max_rounds,
    # available_actions, artifacts, last_output.
    expected_keys = {
        "id", "phase", "type", "trigger", "verdict", "approved",
        "round_extras_key", "round", "loop_max_rounds",
        "available_actions", "artifacts", "last_output",
    }
    assert expected_keys.issubset(set(handoff.keys()))
    assert handoff["round_extras_key"] == "cross_plan_round"
    assert handoff["last_output"]  # truncated last plan markdown


def test_cross_plan_handoff_persists_meta_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause must land ``meta.phase_handoff`` + ``cross_checkpoint``
    on disk so the next subprocess (resume) can hydrate the state.

    The persisted ``meta.json`` MUST also carry
    ``phases.cross_plan`` (rejection round trace) — the in-memory
    session returned by ``run_cross_pipeline`` and the on-disk
    artifact must agree, otherwise post-mortem readers (MCP,
    dashboard) lose visibility into why the run paused. ADR 0038
    review fix: helper saves session, so the round trace has to be
    populated before the pause helper runs.
    """
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
    )
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "awaiting_phase_handoff"
    assert meta["phase_handoff"]["phase"] == "cross_plan"
    # Round trace lands on disk, not just in-memory.
    persisted_cp = meta["phases"]["cross_plan"]
    assert persisted_cp["approved"] is False
    assert len(persisted_cp["rounds"]) == 2
    assert persisted_cp["rounds"][0]["approved"] is False
    ckpt = json.loads(
        (run_dir / "cross_checkpoint.json").read_text(encoding="utf-8"),
    )
    assert ckpt["phase_handoff_pending"] is True
    assert ckpt["phase_handoff_id"] == "cross_plan:cross_plan_round:2"


def test_cross_plan_handoff_snapshots_metrics_at_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause must drop a best-effort ``metrics.json`` snapshot so a
    later ``halt`` decision lands the SDK's ``evidence.json`` next
    to a meaningful metrics file. ADR 0038 review fix: the helper
    captures ``cross_phase_usage`` (cross_plan +
    cross_validate_plan token spend on the rejected plan); per-
    alias rollup is empty here because no sub-pipeline has run."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
    )
    metrics_path = run_dir / "metrics.json"
    assert metrics_path.is_file()
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    # Lock the cross-level shape from ``cross_metrics_dict``: each
    # cross phase entry carries ``kind="cross_level"`` so consumers
    # can distinguish parent-level cross usage from per-alias
    # rollups, and the ``cross_aggregation`` block enumerates the
    # phases that contributed. Tightens the previous "non-empty
    # payload" tripwire so a regression that swapped the shape for
    # an empty/stub mapping would surface here.
    phases = metrics["phases"]
    assert phases["cross_plan"]["kind"] == "cross_level"
    assert phases["cross_validate_plan"]["kind"] == "cross_level"
    cross_phases = metrics["cross_aggregation"]["cross_phases"]
    assert set(cross_phases) >= {"cross_plan", "cross_validate_plan"}
    # Per-alias rollup is empty at pause time (sub-pipelines have
    # not started) — lock that too so a future change that
    # accidentally pulled per-alias metrics into the pause snapshot
    # (mis-timing the rollup) gets caught.
    assert metrics["cross_aggregation"]["sub_pipelines"] == []


# ── Resume — halt ────────────────────────────────────────────────────────────


def test_resume_halt_decision_finalizes_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``halt`` decision short-circuits resume and finalises the cross
    run with ``status=halted`` + ``halt_reason=phase_handoff_halt``."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.md").write_text("# Cross plan\n", encoding="utf-8")
    handoff_id = "cross_plan:cross_plan_round:2"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    _seed_decision(run_dir, handoff_id=handoff_id, action="halt", note="probe")

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
    )
    assert session["status"] == "halted"
    assert session["halt_reason"] == "phase_handoff_halt"
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "phase_handoff_halt"
    # ADR 0037 invariant: terminal lands evidence.json next to meta.json.
    assert (run_dir / "evidence.json").is_file()


# ── Resume — continue ────────────────────────────────────────────────────────


def test_resume_continue_decision_skips_loop_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``continue`` decision accepts the rejected plan on disk and
    proceeds to subtask extraction without re-running cross_plan.

    Locks the audit-trace invariant: ``continue`` falls through to
    the shared meta writer at the tail of ``run_cross_pipeline``,
    so the rejected ``rounds`` from pause time MUST be carried
    through (hydrated from ``resumed_meta``). Without the hydration
    the writer overwrites with an empty list and post-mortem readers
    lose the rejection trace.
    """
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # ADR 0054: continue-resume reads + re-validates the canonical
    # cross_plan.json (not cross_plan.md).
    (run_dir / "cross_plan.json").write_text(
        _cp_json("api", "web"), encoding="utf-8",
    )
    handoff_id = "cross_plan:cross_plan_round:2"
    prior_rounds = [
        {"round": 1, "plan": "v1", "approved": False, "critique": "",
         "review": None},
        {"round": 2, "plan": "v2", "approved": False, "critique": "",
         "review": None},
    ]
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    _seed_decision(run_dir, handoff_id=handoff_id, action="continue")

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
        resumed_meta=_paused_meta(round_n=2, prior_rounds=prior_rounds),
    )
    # plan-only mode terminates at PLAN COMPLETE after the loop.
    assert session["status"] == "awaiting_human_review"
    # Provider must NOT be invoked — continue skips the loop entirely.
    assert provider.plan.calls == []
    assert provider.review.calls == []
    # Checkpoint cleared.
    ckpt = json.loads(
        (run_dir / "cross_checkpoint.json").read_text(encoding="utf-8"),
    )
    assert ckpt.get("phase_handoff_pending") is False
    assert ckpt.get("phase0_done") is True
    # Rejection trace survives — both in-memory return value and
    # persisted meta.json carry rounds 1..2 (and only those; no
    # extra round was run on continue).
    rounds_in_memory = session["phases"]["cross_plan"]["rounds"]
    assert [r["round"] for r in rounds_in_memory] == [1, 2]
    persisted = json.loads(
        (run_dir / "meta.json").read_text(encoding="utf-8"),
    )
    assert [r["round"] for r in persisted["phases"]["cross_plan"]["rounds"]] == [1, 2]


def test_resume_continue_with_waiver_rejected_not_misrouted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``continue_with_waiver`` (ADR 0072) is single-project only. The
    cross_plan producer never publishes it in ``available_actions``, so the
    SDK decide gate refuses it upstream — but the shared
    ``HandoffDecisionAction`` Literal now carries four values, so the cross
    resume dispatcher is no longer three-way exhaustive. A waiver decision
    that reaches resume (here written directly, bypassing the decide gate)
    MUST fail loudly rather than silently mis-routing to the retry branch.
    """
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.json").write_text(
        _cp_json("api", "web"), encoding="utf-8",
    )
    handoff_id = "cross_plan:cross_plan_round:2"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    _seed_decision(
        run_dir, handoff_id=handoff_id,
        action="continue_with_waiver", feedback="ship it anyway",
    )

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    with pytest.raises(RuntimeError, match="does not support action"):
        cross.run_cross_pipeline(
            task="Add field",
            projects=_make_projects(tmp_path),
            output_dir=run_dir,
            provider=provider,
            cross_mode="plan",
            resume_from=run_dir.name,
            resumed_meta=_paused_meta(round_n=2),
        )
    # The retry branch must NOT have run — no extra plan/review round fired.
    assert provider.plan.calls == []
    assert provider.review.calls == []


def test_resume_retry_feedback_handoff_id_parser_fallback_without_resumed_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct callers that resume with a populated cross checkpoint
    but DON'T thread ``resumed_meta`` through still get the correct
    next round number — the orchestrator parses the active round
    out of ``cross_ckpt["phase_handoff_id"]`` as a fallback before
    degrading to ``loop_max_rounds + 1``. Production CLI always
    supplies ``resumed_meta``; the parser is a robustness net so a
    direct caller can't silently regress to the legacy "round 3
    forever" behaviour."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.md").write_text(
        "# Cross plan v3\n", encoding="utf-8",
    )
    # Checkpoint paused @ round 3 (already past the auto budget).
    handoff_id = "cross_plan:cross_plan_round:3"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    _seed_decision(
        run_dir, handoff_id=handoff_id,
        action="retry_feedback", feedback="round 4 feedback",
    )

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json("r4 still failing")],
    )
    # Deliberately omit resumed_meta — the parser fallback must pick
    # up round=3 from the handoff_id and advance to round=4.
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
    )
    assert session["status"] == "awaiting_phase_handoff"
    assert session["phase_handoff"]["id"] == "cross_plan:cross_plan_round:4"
    assert session["phase_handoff"]["round"] == 4


def test_parse_cross_handoff_round_helper() -> None:
    """Direct unit coverage of the parser fallback."""
    from pipeline.cross_project.handoff_payloads import (
        parse_cross_handoff_round as _parse_cross_handoff_round,
    )

    assert _parse_cross_handoff_round("cross_plan:cross_plan_round:7", 99) == 7
    # Malformed id → default.
    assert _parse_cross_handoff_round("not-a-cross-id", 99) == 99
    assert _parse_cross_handoff_round("cross_plan:cross_plan_round:abc", 5) == 5
    # ``None`` is defensive against pre-flight bad state — must not
    # raise.
    assert _parse_cross_handoff_round(None, 11) == 11  # type: ignore[arg-type]


# ── Resume — retry_feedback ──────────────────────────────────────────────────


def test_resume_retry_feedback_runs_one_extra_round_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retry_feedback`` runs ONE more cross_plan round with operator
    feedback in the replan prompt; an approved result clears the
    handoff and proceeds."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.md").write_text(
        "# Cross plan v2\n", encoding="utf-8",
    )
    handoff_id = "cross_plan:cross_plan_round:2"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    _seed_decision(
        run_dir, handoff_id=handoff_id, action="retry_feedback",
        feedback="tighten persistence story",
    )

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
        ],
        review_outputs=[_approved_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
    )
    assert session["status"] == "awaiting_human_review"
    # Exactly one extra plan call + one extra validate call.
    assert len(provider.plan.calls) == 1
    assert len(provider.review.calls) == 1
    ckpt = json.loads(
        (run_dir / "cross_checkpoint.json").read_text(encoding="utf-8"),
    )
    assert ckpt["phase_handoff_pending"] is False
    assert ckpt["phase0_done"] is True


def test_resume_retry_feedback_rejected_re_pauses_with_new_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retry_feedback`` extra round that the reviewer still rejects
    re-pauses with ``handoff_id`` carrying round+1 (relative to the
    ACTIVE handoff round, not ``loop_max_rounds``) so successive
    retries advance through unique IDs."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.md").write_text(
        "# Cross plan v2\n", encoding="utf-8",
    )
    handoff_id = "cross_plan:cross_plan_round:2"
    prior_rounds = [
        {"round": 1, "plan": "v1", "approved": False, "critique": "",
         "review": None},
        {"round": 2, "plan": "v2", "approved": False, "critique": "",
         "review": None},
    ]
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    _seed_decision(
        run_dir, handoff_id=handoff_id, action="retry_feedback",
        feedback="still missing migration",
    )

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json("still failing")],
    )
    # Pass resumed_meta so the orchestrator reads the active handoff
    # round + prior rounds the same way the production CLI does (via
    # ``load_resume_meta``). Without resumed_meta the orchestrator
    # would fall back to ``_effective_plan_rounds + 1`` arithmetic
    # which is exactly the bug this fix closes.
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
        resumed_meta=_paused_meta(round_n=2, prior_rounds=prior_rounds),
    )
    assert session["status"] == "awaiting_phase_handoff"
    new_handoff = session["phase_handoff"]
    assert new_handoff["id"] == "cross_plan:cross_plan_round:3"
    assert new_handoff["round"] == 3
    assert new_handoff["loop_max_rounds"] == 2
    assert new_handoff["approved"] is False
    # Audit trace stitches new retry onto rounds 1..2 from prior
    # subprocess — overall 3 rounds preserved, not collapsed to the
    # latest retry alone.
    persisted_rounds = session["phases"]["cross_plan"]["rounds"]
    assert [r["round"] for r in persisted_rounds] == [1, 2, 3]


def test_resume_retry_feedback_multi_cycle_advances_round_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive ``retry_feedback`` decisions on the SAME run:

    - auto budget 2 rejected → pause @ round 2
    - operator retry 1 → run round 3 (rejected) → pause @ round 3
    - operator retry 2 → run round 4 (rejected) → pause @ round 4

    Locks the two invariants the code review surfaced:

    1. Each retry advances the handoff round from the *active*
       handoff (not ``loop_max_rounds + 1``), so decision artifacts
       under ``phase_handoff_decisions/`` get unique IDs.
    2. ``meta.phases.cross_plan.rounds`` stitches rounds 1..4
       across subprocess boundaries — audit trace is preserved.
    """
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.md").write_text("# v\n", encoding="utf-8")
    projects = _make_projects(tmp_path)

    # First retry cycle: paused @ round 2 from a prior subprocess.
    handoff_id_2 = "cross_plan:cross_plan_round:2"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id_2)
    _seed_decision(
        run_dir, handoff_id=handoff_id_2,
        action="retry_feedback", feedback="round 3 feedback",
    )
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json("r3 still bad")],
    )
    session1 = cross.run_cross_pipeline(
        task="Add field",
        projects=projects,
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
        resumed_meta=_paused_meta(
            round_n=2,
            prior_rounds=[
                {"round": 1, "plan": "v1", "approved": False,
                 "critique": "", "review": None},
                {"round": 2, "plan": "v2", "approved": False,
                 "critique": "", "review": None},
            ],
        ),
    )
    # Cycle 1 ends with re-pause @ round 3.
    assert session1["phase_handoff"]["id"] == "cross_plan:cross_plan_round:3"
    rounds_after_cycle1 = session1["phases"]["cross_plan"]["rounds"]
    assert [r["round"] for r in rounds_after_cycle1] == [1, 2, 3]
    persisted = json.loads(
        (run_dir / "meta.json").read_text(encoding="utf-8"),
    )
    assert persisted["phase_handoff"]["round"] == 3

    # Second retry cycle: paused @ round 3, operator picks
    # retry_feedback again. Active handoff id advances.
    handoff_id_3 = "cross_plan:cross_plan_round:3"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id_3)
    _seed_decision(
        run_dir, handoff_id=handoff_id_3,
        action="retry_feedback", feedback="round 4 feedback",
    )
    provider2 = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json("r4 still bad")],
    )
    session2 = cross.run_cross_pipeline(
        task="Add field",
        projects=projects,
        output_dir=run_dir,
        provider=provider2,
        cross_mode="plan",
        resume_from=run_dir.name,
        resumed_meta=_paused_meta(
            round_n=3,
            prior_rounds=rounds_after_cycle1,
        ),
    )
    # Cycle 2 must advance to round 4 — NOT collide back at round 3.
    assert session2["phase_handoff"]["id"] == "cross_plan:cross_plan_round:4"
    assert session2["phase_handoff"]["round"] == 4
    # And rounds 1..4 all preserved in the persisted trace.
    rounds_after_cycle2 = session2["phases"]["cross_plan"]["rounds"]
    assert [r["round"] for r in rounds_after_cycle2] == [1, 2, 3, 4]
    persisted2 = json.loads(
        (run_dir / "meta.json").read_text(encoding="utf-8"),
    )
    assert persisted2["phases"]["cross_plan"]["rounds"][-1]["round"] == 4


# ── Resume — error paths ─────────────────────────────────────────────────────


def test_real_sdk_decide_writes_artifact_consumable_by_cross_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end SDK parity check: write a real paused ``meta.json``
    + cross checkpoint, call the production
    :func:`sdk.phase_handoff.phase_handoff_decide` (NOT the bypass
    ``_seed_decision`` helper), and verify the cross-resume path
    consumes the artifact the same way it would in production.

    Catches drifts where ``orcho_phase_handoff_decide`` would
    validate / persist cross ``handoff_id`` shape differently from
    the bypass tests, or where the cross resume reader couldn't
    locate the SDK-written artifact path."""
    from sdk.phase_handoff import phase_handoff_decide

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # ADR 0054: continue-resume reads + re-validates cross_plan.json.
    (run_dir / "cross_plan.json").write_text(
        _cp_json("api", "web"), encoding="utf-8",
    )
    # Lay down the paused meta.json the SDK reads to validate
    # handoff_id against active payload.
    paused_meta = _paused_meta(round_n=2)
    (run_dir / "meta.json").write_text(
        json.dumps(paused_meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    handoff_id = "cross_plan:cross_plan_round:2"
    _write_cross_checkpoint_with_handoff(run_dir, handoff_id=handoff_id)
    # Point the SDK at our isolated tmp_path runs dir.
    runs_dir = run_dir.parent

    # Call the public SDK exactly the way ``orcho_phase_handoff_decide``
    # (MCP tool) routes — same validation, same artifact placement.
    phase_handoff_decide(
        run_dir.name,
        handoff_id=handoff_id,
        action="continue",
        runs_dir=runs_dir,
        cwd=None,
    )

    # SDK landed the artifact under phase_handoff_decisions/.
    decisions = list((run_dir / "phase_handoff_decisions").glob("*.json"))
    assert len(decisions) == 1, decisions
    artifact = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert artifact["action"] == "continue"
    assert artifact["handoff_id"] == handoff_id
    assert artifact["phase"] == "cross_plan"

    # Cross-resume must consume the SDK-written artifact identically
    # to the bypass helper's output — exercise the full resume path.
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    projects = _make_projects(tmp_path)
    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=projects,
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        resume_from=run_dir.name,
        resumed_meta=paused_meta,
    )
    # ``continue`` accepts the rejected plan + jumps past the loop.
    assert session["status"] == "awaiting_human_review"
    assert provider.plan.calls == []
    assert provider.review.calls == []


def test_resume_with_pending_handoff_missing_decision_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkpoint flags handoff as pending but no decision artifact —
    the orchestrator must abort with an actionable error rather than
    silently dropping the pause."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cross_plan.md").write_text("# plan\n", encoding="utf-8")
    _write_cross_checkpoint_with_handoff(
        run_dir, handoff_id="cross_plan:cross_plan_round:2",
    )
    # No _seed_decision call — decision artifact deliberately absent.

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    with pytest.raises(RuntimeError, match="no decision artifact"):
        cross.run_cross_pipeline(
            task="Add field",
            projects=_make_projects(tmp_path),
            output_dir=run_dir,
            provider=provider,
            cross_mode="plan",
            resume_from=run_dir.name,
        )


# ── ADR 0054 — stale-plan guard on schema-invalid final round ─────────────────


def _valid_cross_plan_json(summary: str, *, aliases=("api", "web")) -> str:
    """One schema-valid cross-plan JSON object covering ``aliases``."""
    subtasks = []
    for i, alias in enumerate(aliases):
        subtasks.append({
            "alias":       alias,
            "goal":        f"{alias} goal",
            "spec":        f"{summary} {alias} spec",
            "depends_on":  ([aliases[0]] if i > 0 else []),
        })
    return json.dumps({
        "short_summary":        summary,
        "interface_contract":   "shared POST /api/users contract",
        "implementation_order": [f"change {a}" for a in aliases],
        "subtasks":             subtasks,
    })


def test_invalid_final_round_pause_omits_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0054 stale-plan guard: round 1 parses schema-valid but QA
    rejects it (cross_plan.json now holds round 1), then round 2 emits
    INVALID JSON (synthetic reject) and the budget exhausts → pause.

    ``continue`` MUST be withheld (the paused round 2 is not schema-valid),
    so an operator cannot accidentally dispatch the OLDER round-1 plan that
    QA already rejected. cross_plan.json still holds round 1 (latest-valid),
    which is exactly why continue must be off.
    """
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"

    provider = _ScriptedProvider(
        plan_outputs=[
            _valid_cross_plan_json("round-one-plan"),   # valid, QA-rejected
            "this is not valid cross-plan JSON at all",  # invalid → synthetic
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
    )

    handoff = session["phase_handoff"]
    assert handoff["phase"] == "cross_plan"
    assert "continue" not in handoff["available_actions"], (
        f"continue must be withheld on a schema-invalid final round; "
        f"got {handoff['available_actions']}"
    )
    assert handoff["available_actions"] == ["retry_feedback", "halt"]

    # The on-disk canonical plan is round 1 (latest schema-valid), NOT the
    # invalid round 2 — proving continue would have dispatched a stale plan.
    cj = json.loads((run_dir / "cross_plan.json").read_text(encoding="utf-8"))
    assert cj["short_summary"] == "round-one-plan"
    assert {s["alias"] for s in cj["subtasks"]} == {"api", "web"}


def test_valid_rejected_final_round_pause_offers_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart: when the paused (final) round is schema-VALID but
    QA-rejected, ``continue`` IS offered — cross_plan.json holds THIS round's
    plan (persisted pre-QA), so continuing dispatches the right one."""
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    run_dir = tmp_path / "run"

    provider = _ScriptedProvider(
        plan_outputs=[
            _valid_cross_plan_json("round-one-plan"),
            _valid_cross_plan_json("round-two-plan"),
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Add field",
        projects=_make_projects(tmp_path),
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
    )

    handoff = session["phase_handoff"]
    assert "continue" in handoff["available_actions"]
    assert handoff["available_actions"] == [
        "continue", "retry_feedback", "halt",
    ]
    # cross_plan.json holds the latest valid round (round 2).
    cj = json.loads((run_dir / "cross_plan.json").read_text(encoding="utf-8"))
    assert cj["short_summary"] == "round-two-plan"
