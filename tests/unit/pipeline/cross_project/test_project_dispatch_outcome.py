"""Durable child-outcome classification at the cross dispatch boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipeline.cross_project import project_dispatch
from pipeline.cross_project.project_dispatch import (
    DispatchPorts,
    ProjectDispatchContext,
    run_project_dispatch,
)


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
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

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


def test_resume_retries_embedded_done_without_physical_child(tmp_path, monkeypatch) -> None:
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
    def _child(request):
        calls.append(request.project_alias)
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", _child)

    result = run_project_dispatch(ctx)

    assert calls == ["done", "failed", "unknown"]
    assert result.blocking_aliases == ()
    assert ctx.session["phases"]["projects"]["done"] == {"status": "done", "phases": {}}


def test_residual_handoff_on_failed_child_is_not_paused(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("core",))
    monkeypatch.setattr(
        project_dispatch,
        "run_project_pipeline",
        lambda request: SimpleNamespace(
            session={
                "status": "failed",
                "phase_handoff": {"id": "child-1", "available_actions": ["continue"]},
                "phases": {},
            }
        ),
    )

    result = run_project_dispatch(ctx)

    assert result == project_dispatch.ProjectDispatchResult(paused=False, blocking_aliases=("core",))
