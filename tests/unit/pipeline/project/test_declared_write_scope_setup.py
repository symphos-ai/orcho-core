"""Declared-write-scope hydration at the project state boundary."""

from pathlib import Path

from agents.entities import SubTask
from agents.protocols import SessionMode
from pipeline.engine.declared_write_scope import (
    DECLARED_WRITE_SCOPE_EXTRAS_KEY,
    DeclaredWriteOriginKind,
)
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.project.state_setup import StateInputs, build_pipeline_state
from pipeline.project.types import PresentationPolicy


def _inputs(tmp_path: Path, **overrides) -> StateInputs:
    values = dict(
        task="task", project_path=tmp_path,
        plugin=PluginConfig(allowed_modifications=["uv.lock — generated"]),
        phase_config=None, agent_registry=None, output_dir=None, dry_run=True,
        session={}, session_ts="run", git_cwd=str(tmp_path),
        change_handoff="uncommitted", cross_handoff_text="", plan_source="local",
        handoff_path=None, auto_waiver_allowed=False, followup_seed_count=0,
        ckpt=None, attachments=None, session_mode=SessionMode.AUTO,
        implement_model="m", repair_model="m", repair_escalation_model="m",
        chain_same_model_only=False, presentation=PresentationPolicy.SILENT,
        render_phase_outputs=False, from_run_plan_loaded=None,
        followup_parent_run_id=None, from_run_plan_parent_dir=None,
        from_run_plan_stripped=(),
    )
    values.update(overrides)
    return StateInputs(**values)


def test_cross_first_and_cold_state_have_equal_typed_scope(tmp_path: Path) -> None:
    first = build_pipeline_state(_inputs(
        tmp_path, plan_source="cross", cross_handoff_text="first prose",
        cross_declared_files=("a.py", "tests/test_a.py"),
    )).state
    cold = build_pipeline_state(_inputs(
        tmp_path, plan_source="cross", cross_handoff_text="changed prose",
        cross_declared_files=("a.py", "tests/test_a.py"),
    )).state
    first_scope = first.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY]
    assert first_scope == cold.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY]
    assert first_scope.patterns == ("a.py", "tests/test_a.py", "uv.lock")
    assert first.parsed_plan is None
    assert {origin.kind for rule in first_scope.rules for origin in rule.origins} == {
        DeclaredWriteOriginKind.CROSS_UNIT,
        DeclaredWriteOriginKind.PLUGIN_ALLOWANCE,
    }


def test_mono_plan_continuation_hydrates_existing_scope_patterns(tmp_path: Path) -> None:
    plan = ParsedPlan(
        subtasks=(SubTask(id="one", goal="g", owned_files=("src/a.py",)),),
        source="json",
        short_summary="resume plan",
        planning_context="context",
        owned_files=("README.md",),
        allowed_modifications=("docs/** — generated",),
    )
    parent = tmp_path / "parent"
    parent.mkdir()
    state = build_pipeline_state(_inputs(
        tmp_path,
        from_run_plan_loaded=plan,
        from_run_plan_parent_dir=parent,
    )).state
    assert state.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY].patterns == (
        "README.md", "docs/**", "src/a.py", "uv.lock",
    )


def test_mono_resume_artifact_hydrates_same_scope_patterns(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = ParsedPlan(
        subtasks=(SubTask(id="one", goal="g", owned_files=("src/a.py",)),),
        source="json",
        short_summary="resume plan",
        planning_context="context",
        owned_files=("README.md",),
        allowed_modifications=("docs/** — generated",),
    )
    write_parsed_plan_artifact(run_dir, plan, attempt=1)
    state = build_pipeline_state(_inputs(
        tmp_path,
        output_dir=run_dir,
        resume_completed_phases=frozenset({"plan"}),
        resume_requested=True,
    )).state
    assert state.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY].patterns == (
        "README.md", "docs/**", "src/a.py", "uv.lock",
    )
