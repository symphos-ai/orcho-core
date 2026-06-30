"""Tests for the generic resume-artifact bootstrap (ADR 0079).

Covers the runner's six-category classification, the owned
``RESUME_PLAN_REQUIRED_KEY`` marker + provenance discipline, the no-overwrite
rule for explicit plans, and the two falsifier paths that prove a required
failure surfaces an *instructive* ``subtask_dag`` error (missing vs unreadable)
rather than the generic empty-plan line.

A deliberately non-plan ``ResumeArtifactSpec`` proves the runner loop is
generic — not hardcoded to ``parsed_plan``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from agents.entities import SubTask
from agents.protocols import SessionMode
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.project.resume_artifacts import (
    RESUME_PLAN_REQUIRED_KEY,
    BootstrapContext,
    ResumeArtifactSpec,
    bootstrap_resume_artifacts,
)
from pipeline.project.state_setup import StateInputs, build_pipeline_state
from pipeline.project.types import PresentationPolicy
from pipeline.runtime import PipelineState

# ── fixtures ──────────────────────────────────────────────────────────────


def _plan(*ids: str) -> ParsedPlan:
    subtasks = tuple(
        SubTask(id=i, goal=f"Do {i}", spec="spec") for i in (ids or ("t1",))
    )
    return ParsedPlan(
        short_summary="plan",
        planning_context="ctx",
        subtasks=subtasks,
        source="json",
    )


def _state(tmp_path: Path, output_dir: Path | None = None) -> PipelineState:
    return PipelineState(
        task="t",
        project_dir=str(tmp_path),
        plugin=PluginConfig(),
        output_dir=output_dir,
    )


def _state_inputs(tmp_path: Path, **overrides) -> StateInputs:
    base: dict = {
        "task": "do the thing",
        "project_path": tmp_path,
        "plugin": PluginConfig(),
        "phase_config": None,
        "agent_registry": None,
        "output_dir": tmp_path / "run",
        "dry_run": True,
        "session": {},
        "session_ts": "20260610_000000",
        "git_cwd": str(tmp_path),
        "change_handoff": "uncommitted",
        "cross_handoff_text": "",
        "plan_source": "local",
        "handoff_path": None,
        "auto_waiver_allowed": False,
        "followup_seed_count": 0,
        "ckpt": None,
        "attachments": None,
        "session_mode": SessionMode.AUTO,
        "implement_model": "m",
        "repair_model": "m",
        "repair_escalation_model": "m",
        "chain_same_model_only": False,
        "presentation": PresentationPolicy.SILENT,
        "render_phase_outputs": False,
        "from_run_plan_loaded": None,
        "followup_parent_run_id": None,
        "from_run_plan_parent_dir": None,
        "from_run_plan_stripped": (),
    }
    base.update(overrides)
    return StateInputs(**base)


# A deliberately non-plan spec: proves the runner is generic.
def _fake_spec() -> ResumeArtifactSpec:
    def _load(run_dir: Path) -> str:
        path = run_dir / "fake.txt"
        if not path.is_file():
            raise ValueError("fake artifact missing")
        return path.read_text(encoding="utf-8")

    def _project(state, value) -> None:
        state.extras["fake_value"] = value

    return ResumeArtifactSpec(
        name="fake",
        phase="fake_phase",
        artifact="fake.txt",
        load=_load,
        project=_project,
        required_when=lambda ctx: "fake_phase" in ctx.completed_phases,
        already_present=lambda s: "fake_value" in s.extras,
    )


# ── 1) fresh run: strict no-op ────────────────────────────────────────────


class TestFreshRunNoOp:
    def test_empty_completed_no_artifact_is_strict_noop(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        state = _state(tmp_path, output_dir=run_dir)
        before = dict(state.extras)

        result = bootstrap_resume_artifacts(
            state, run_dir, completed_phases=frozenset(),
        )

        assert state.extras == before  # no marker, no provenance
        assert RESUME_PLAN_REQUIRED_KEY not in state.extras
        assert state.parsed_plan is None
        assert result.missing_optional == ["parsed_plan"]
        # No files / directories created by the bootstrap.
        assert list(run_dir.iterdir()) == []
        assert not (run_dir / "parsed_plan.json").exists()

    def test_run_dir_none_is_noop(self, tmp_path) -> None:
        state = _state(tmp_path, output_dir=None)
        before = dict(state.extras)

        result = bootstrap_resume_artifacts(
            state, None, completed_phases=frozenset({"plan"}),
        )

        assert state.extras == before
        assert result.loaded == []
        assert result.missing_required == []


# ── 2) generic loop via a fake non-plan spec ──────────────────────────────


class TestGenericLoopFakeSpec:
    def test_fake_spec_loads_and_projects(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "fake.txt").write_text("payload", encoding="utf-8")
        state = _state(tmp_path, output_dir=run_dir)

        result = bootstrap_resume_artifacts(
            state,
            run_dir,
            completed_phases=frozenset(),
            specs=(_fake_spec(),),
        )

        assert result.loaded == ["fake"]
        assert state.extras["fake_value"] == "payload"
        # The loop is NOT hardcoded on parsed_plan: a non-plan spec drove it.
        assert state.parsed_plan is None

    def test_fake_spec_already_present_skips_without_overwrite(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "fake.txt").write_text("disk-value", encoding="utf-8")
        state = _state(tmp_path, output_dir=run_dir)
        state.extras["fake_value"] = "in-memory-value"

        result = bootstrap_resume_artifacts(
            state,
            run_dir,
            completed_phases=frozenset(),
            specs=(_fake_spec(),),
        )

        assert result.skipped_already_present == ["fake"]
        assert result.loaded == []
        assert state.extras["fake_value"] == "in-memory-value"  # not clobbered

    def test_fake_spec_required_sets_marker(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "fake.txt").write_text("payload", encoding="utf-8")
        state = _state(tmp_path, output_dir=run_dir)

        # required_when keys off 'fake_phase' — generic, not plan-specific.
        bootstrap_resume_artifacts(
            state,
            run_dir,
            completed_phases=frozenset({"fake_phase"}),
            specs=(_fake_spec(),),
        )

        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True


# ── 3) checkpoint resume: valid artifact loaded ───────────────────────────


class TestCheckpointResumeLoaded:
    def test_plan_completed_valid_artifact_is_loaded(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _plan("t1", "t2"), attempt=1)
        state = _state(tmp_path, output_dir=run_dir)
        assert state.parsed_plan is None

        result = bootstrap_resume_artifacts(
            state, run_dir, completed_phases=frozenset({"plan"}),
        )

        assert result.loaded == ["parsed_plan"]
        assert [s.id for s in state.parsed_plan.subtasks] == ["t1", "t2"]
        assert state.plan_markdown
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "source": "artifact",
        }
        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True


# ── 4 & 5) falsifiers: required failure → instructive subtask_dag error ────


def _stop_message_for(state: PipelineState) -> str:
    """Drive the real subtask_dag implement guard and return its stop reason."""
    from types import SimpleNamespace

    from pipeline.phases.builtin.subtask_dag import _run_subtask_dag_implement

    _run_subtask_dag_implement(state, SimpleNamespace(), None)
    assert state.halt is True
    return state.halt_reason


_GENERIC = (
    "implementation_execution=subtask_dag requires a parsed plan "
    "with at least one required subtask"
)


class TestFalsifierMissingArtifact:
    def test_missing_required_then_instructive_subtask_dag_error(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "20260610_missing"
        run_dir.mkdir()
        state = _state(tmp_path, output_dir=run_dir)

        result = bootstrap_resume_artifacts(
            state, run_dir, completed_phases=frozenset({"plan"}),
        )

        assert result.missing_required == ["parsed_plan"]
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "status": "missing",
        }
        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True
        assert state.parsed_plan is None

        message = _stop_message_for(state)
        assert message != _GENERIC
        assert "parsed_plan.json" in message
        assert run_dir.name in message
        assert "missing" in message


class TestFalsifierCorruptArtifact:
    def test_corrupt_required_then_instructive_unreadable_error(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "20260610_corrupt"
        run_dir.mkdir()
        # Present but unreadable: not valid JSON for the artifact loader.
        (run_dir / "parsed_plan.json").write_text("{not json", encoding="utf-8")
        state = _state(tmp_path, output_dir=run_dir)

        result = bootstrap_resume_artifacts(
            state, run_dir, completed_phases=frozenset({"plan"}),
        )

        assert result.corrupt_required == ["parsed_plan"]
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "status": "corrupt",
        }
        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True
        # No markdown fallback: the corrupt artifact did NOT hydrate a plan.
        assert state.parsed_plan is None

        message = _stop_message_for(state)
        assert message != _GENERIC
        assert "parsed_plan.json" in message
        assert "unreadable" in message


# ── 6) no-overwrite for an explicit in-memory plan ────────────────────────


class TestNoOverwriteExplicitPlan:
    def test_already_present_plan_is_skipped_and_identity_preserved(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _plan("disk"), attempt=1)
        state = _state(tmp_path, output_dir=run_dir)
        explicit = _plan("explicit")
        state.parsed_plan = explicit  # e.g. --from-run-plan / same-process

        result = bootstrap_resume_artifacts(
            state, run_dir, completed_phases=frozenset({"plan"}),
        )

        assert result.skipped_already_present == ["parsed_plan"]
        assert result.loaded == []
        assert state.parsed_plan is explicit  # identity preserved, not reloaded
        # No loaded-provenance written for a skipped spec.
        assert "resume_artifacts" not in state.extras


# ── 7) build_pipeline_state integration ───────────────────────────────────


class TestBuildPipelineStateIntegration:
    def test_resume_completed_plan_hydrates_for_implement(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _plan("t1"), attempt=1)
        inputs = _state_inputs(
            tmp_path,
            output_dir=run_dir,
            resume_completed_phases=frozenset({"plan"}),
        )

        state = build_pipeline_state(inputs).state

        # IMPLEMENT sees the recovered plan.
        assert state.parsed_plan.subtasks[0].id == "t1"
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "source": "artifact",
        }
        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True

    def test_resume_completed_plan_missing_marks_without_raising(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        inputs = _state_inputs(
            tmp_path,
            output_dir=run_dir,
            resume_completed_phases=frozenset({"plan"}),
            dry_run=False,  # so the subtask_dag guard actually fires below
        )

        # build_pipeline_state must NOT raise on the missing required artifact.
        state = build_pipeline_state(inputs).state

        assert state.parsed_plan is None
        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "status": "missing",
        }
        # The authoritative error lives in subtask_dag, not build.
        assert _stop_message_for(state) != _GENERIC


def test_bootstrap_context_carries_completed_and_run_dir(tmp_path) -> None:
    ctx = BootstrapContext(
        completed_phases=frozenset({"plan"}), run_dir=tmp_path,
    )
    assert "plan" in ctx.completed_phases
    assert ctx.run_dir == tmp_path
    # Frozen value object.
    assert dataclasses.is_dataclass(ctx)


class TestHydrateParsedPlanFromOutputDirWrapper:
    """The public ``hydrate_parsed_plan_from_output_dir`` compatibility entry
    point stays importable from ``state_setup`` and keeps its bool contract by
    delegating to the shared projector (no second loader)."""

    def test_importable_and_loads(self, tmp_path) -> None:
        # Pin the public import surface (regression guard: it was removed once).
        from pipeline.project.state_setup import (
            hydrate_parsed_plan_from_output_dir,
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _plan("t1"), attempt=1)
        state = _state(tmp_path, output_dir=run_dir)

        assert hydrate_parsed_plan_from_output_dir(state, run_dir) is True
        assert state.parsed_plan.subtasks[0].id == "t1"
        assert state.plan_markdown
        # Delegates to the shared projector → same provenance shape.
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "source": "artifact",
        }

    def test_noop_returns_false(self, tmp_path) -> None:
        from pipeline.project.state_setup import (
            hydrate_parsed_plan_from_output_dir,
        )

        # output_dir None → False.
        assert hydrate_parsed_plan_from_output_dir(
            _state(tmp_path, output_dir=None), None,
        ) is False

        # Missing artifact → False, no markdown fallback.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        missing_state = _state(tmp_path, output_dir=run_dir)
        assert hydrate_parsed_plan_from_output_dir(
            missing_state, run_dir,
        ) is False
        assert missing_state.parsed_plan is None

        # Already-present plan → False, identity preserved (no overwrite).
        explicit = _plan("explicit")
        present_state = _state(tmp_path, output_dir=run_dir)
        present_state.parsed_plan = explicit
        assert hydrate_parsed_plan_from_output_dir(
            present_state, run_dir,
        ) is False
        assert present_state.parsed_plan is explicit
