"""Durable child-outcome classification at the cross dispatch boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pipeline.cross_project import project_dispatch
from pipeline.cross_project.project_dispatch import (
    DispatchPorts,
    ProjectDispatchContext,
    _classify_child_outcome,
    run_project_dispatch,
)


@pytest.mark.parametrize(
    ("session", "kind", "reason"),
    [
        ({"status": "done"}, "success", "status:done"),
        ({"status": "success"}, "success", "status:success"),
        ({"status": "completed"}, "success", "status:completed"),
        (
            {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "p"}},
            "pause",
            "status:awaiting_phase_handoff",
        ),
        ({"status": "failed"}, "failure", "status:failed"),
        ({"status": "halted"}, "failure", "status:halted"),
        (
            {
                "status": "halted",
                "halt_reason": "final_acceptance_rejected",
                "phases": {
                    "final_acceptance": {
                        "verdict": "REJECTED",
                        "ship_ready": False,
                    },
                },
            },
            "release_rejected",
            "status:halted:final_acceptance_rejected",
        ),
        (
            {
                "status": "halted",
                "halt_reason": "final_acceptance_rejected",
                "phases": {},
            },
            "failure",
            "status:halted",
        ),
        ({"status": "interrupted"}, "failure", "status:interrupted"),
        ({}, "failure", "status_missing"),
        ({"status": None}, "failure", "status_not_string"),
        ({"status": "mystery"}, "failure", "status_unknown:mystery"),
        (
            {"status": "awaiting_phase_handoff"},
            "failure",
            "pause_payload_missing_or_invalid",
        ),
        (
            {"status": "awaiting_phase_handoff", "phase_handoff": "not a mapping"},
            "failure",
            "pause_payload_missing_or_invalid",
        ),
    ],
)
def test_classify_child_outcome_is_bounded_and_fail_closed(session, kind, reason) -> None:
    outcome = _classify_child_outcome(session)
    assert (outcome.kind, outcome.reason) == (kind, reason)


def _context(tmp_path, aliases=("core", "mcp")) -> ProjectDispatchContext:
    projects = {}
    for alias in aliases:
        project = tmp_path / alias
        project.mkdir()
        projects[alias] = project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return ProjectDispatchContext(
        task="test dispatch outcomes",
        projects=projects,
        task_plan=None,
        resume_from="resume-id",
        dry_run=False,
        max_rounds=1,
        code_model="test",
        phase_config=None,
        child_profile=object(),
        requested_profile_name="test",
        has_global_plan=False,
        provider=MagicMock(),
        hypothesis_enabled=False,
        followup_session_seeds_per_alias=None,
        run_dir=run_dir,
        output_dir=False,
        plan_output="",
        plan_review_dict=None,
        cross_ckpt={"sub_status": {}},
        session={"phases": {"projects": {}}},
        cross_phase_usage={},
        ports=DispatchPorts(MagicMock(), MagicMock(), MagicMock()),
        terminal=False,
    )


def test_normal_return_halted_preserves_session_and_blocks_dispatch(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("core",))
    halted = {
        "status": "halted",
        "halt_reason": "operator requested stop",
        "phases": {"implement": {"status": "halted"}},
    }
    monkeypatch.setattr(
        project_dispatch,
        "run_project_pipeline",
        lambda request: SimpleNamespace(session=halted),
    )

    result = run_project_dispatch(ctx)

    assert result == project_dispatch.ProjectDispatchResult(
        paused=False, blocking_aliases=("core",),
    )
    assert ctx.cross_ckpt["sub_status"] == {"core": "failed"}
    assert ctx.session["phases"]["projects"]["core"] is halted


def test_exception_records_failure_and_continues_to_later_alias(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path)
    calls: list[str] = []

    def _child(request):
        calls.append(request.project_alias)
        if request.project_alias == "core":
            raise RuntimeError("broken child")
        return SimpleNamespace(session={"status": "done", "phases": {}})

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", _child)

    result = run_project_dispatch(ctx)

    assert calls == ["core", "mcp"]
    assert result.blocking_aliases == ("core",)
    assert ctx.cross_ckpt["sub_status"] == {"core": "failed", "mcp": "done"}
    assert ctx.session["phases"]["projects"]["core"] == {
        "status": "failed",
        "error": "RuntimeError: broken child",
        "phases": {},
    }


def test_resume_retries_failed_and_unknown_but_skips_only_done(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("done", "failed", "unknown"))
    ctx.cross_ckpt["sub_status"] = {
        "done": "done", "failed": "failed", "unknown": "wat",
    }
    ctx.session["phases"]["projects"] = {
        "done": {"status": "done"},
        "failed": {"status": "failed"},
        "unknown": {"status": "wat"},
    }
    calls: list[str] = []
    monkeypatch.setattr(
        project_dispatch,
        "run_project_pipeline",
        lambda request: calls.append(request.project_alias)
        or SimpleNamespace(session={"status": "done", "phases": {}}),
    )

    result = run_project_dispatch(ctx)

    assert calls == ["failed", "unknown"]
    assert result.blocking_aliases == ()
    assert ctx.session["phases"]["projects"]["done"] == {"status": "done"}
