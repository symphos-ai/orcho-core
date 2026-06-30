"""End-to-end lineage header for checkpoint resume of a follow-up child (F3).

Drives the real ``run_setup.print_pipeline_header`` (not just the pure
``render_run_header`` helper) with the lineage extracted from a follow-up
child's own ``meta.json`` via ``build_checkpoint_followup_lineage`` — the
exact wiring the CLI uses on ``orcho run --resume <child>``.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.protocols import SessionMode
from pipeline.control.resume_context import (
    ResumedMeta,
    build_checkpoint_followup_lineage,
)
from pipeline.plugins import PluginConfig
from pipeline.project.run_setup import print_pipeline_header
from pipeline.project.types import PresentationPolicy
from pipeline.runtime.profile import LoopStep, Profile
from pipeline.runtime.roles import FullCycleDepth, ProfileKind
from pipeline.runtime.steps import PhaseStep


def _child_meta(tmp_path: Path) -> ResumedMeta:
    meta = {
        "resume_mode": "followup",
        "parent_run_id": "20260606_232511",
        "parent_run_dir": "/runs/20260606_232511",
        "parent_status": "awaiting_phase_handoff",
        "status": "interrupted",
        "base_task": "harden resume UX",
        "phase_handoff": {"id": "review_changes:repair_round:1"},
    }
    child_dir = tmp_path / "runs" / "20260607_113234"
    child_dir.mkdir(parents=True)
    (child_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return ResumedMeta(path=child_dir / "meta.json", meta=meta)


def _advanced_profile() -> Profile:
    return Profile(
        name="advanced",
        kind=ProfileKind.FULL_CYCLE,
        variant=FullCycleDepth.ADVANCED.value,
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(phase="validate_plan"),
                ),
                until="validate_plan.approved",
                max_rounds=2,
            ),
            PhaseStep(phase="implement"),
            LoopStep(
                steps=(
                    PhaseStep(phase="review_changes"),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.approved",
                max_rounds=2,
            ),
            PhaseStep(phase="final_acceptance"),
        ),
    )


def _seed_retry_feedback_decision(
    run_dir: Path,
    *,
    handoff_id: str,
    phase: str,
) -> None:
    from sdk.phase_handoff import safe_handoff_id

    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    (decisions_dir / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "handoff_id": handoff_id,
                "phase": phase,
                "action": "retry_feedback",
                "feedback": "fix the rejected finding",
                "note": None,
                "decided_at": "2026-06-10T12:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_checkpoint_followup_header_shows_lineage(tmp_path, capsys) -> None:
    resumed = _child_meta(tmp_path)
    lineage = build_checkpoint_followup_lineage(resumed)
    assert lineage is not None

    output_dir = resumed.path.parent
    print_pipeline_header(
        presentation=PresentationPolicy.TERMINAL,
        project_path=tmp_path / "proj",
        task="harden resume UX",
        plan_model="m", implement_model="m", review_model="m",
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        max_rounds=1, do_plan=True, plugin=PluginConfig(),
        output_dir=output_dir,
        # Wired exactly as the CLI does for a checkpoint follow-up child.
        followup_parent_run_id=lineage.parent_run_id,
        followup_parent_status=lineage.parent_status,
        followup_child_status=lineage.child_status,
        followup_active_handoff_id=lineage.active_handoff_id,
        resume_from="20260607_113234",
    )
    out = capsys.readouterr().out
    assert "follow-up of 20260606_232511" in out
    assert "Parent status" in out
    assert "awaiting_phase_handoff" in out
    assert "This run status" in out
    assert "interrupted" in out
    assert "Active handoff" in out
    assert "review_changes:repair_round:1" in out


def test_retry_feedback_resume_header_highlights_repair_target(
    tmp_path,
    capsys,
) -> None:
    output_dir = tmp_path / "runs" / "20260610_173948"
    output_dir.mkdir(parents=True)
    handoff_id = "review_changes:repair_round:1"
    meta = {
        "status": "awaiting_phase_handoff",
        "phase_handoff": {
            "id": handoff_id,
            "phase": "review_changes",
            "round": 1,
            "loop_max_rounds": 1,
        },
    }
    (output_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _seed_retry_feedback_decision(
        output_dir,
        handoff_id=handoff_id,
        phase="review_changes",
    )

    print_pipeline_header(
        presentation=PresentationPolicy.TERMINAL,
        project_path=tmp_path / "proj",
        task="repair rejected review",
        plan_model="m",
        implement_model="m",
        review_model="m",
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        max_rounds=1,
        do_plan=True,
        plugin=PluginConfig(),
        output_dir=output_dir,
        profile_obj=_advanced_profile(),
        resume_from=output_dir.name,
    )

    out = capsys.readouterr().out
    assert "▶ repair_changes" in out
    assert "▶ plan" not in out
    assert "· plan" in out


def test_non_followup_checkpoint_has_no_lineage(tmp_path, capsys) -> None:
    output_dir = tmp_path / "runs" / "20260607_120000"
    output_dir.mkdir(parents=True)
    print_pipeline_header(
        presentation=PresentationPolicy.TERMINAL,
        project_path=tmp_path / "proj",
        task="plain run",
        plan_model="m", implement_model="m", review_model="m",
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        max_rounds=1, do_plan=True, plugin=PluginConfig(),
        output_dir=output_dir,
        resume_from="20260607_120000",
    )
    out = capsys.readouterr().out
    assert "Parent status" not in out
    assert "Active handoff" not in out
