from __future__ import annotations

import pytest

from pipeline.checkpoint import CheckpointStore, LoopCursorRecord, PhaseCheckpointRecord
from pipeline.plugins import PluginConfig
from pipeline.project.loop_resume import (
    inspect_checkpoint_resume,
    resolve_loop_resume,
)
from pipeline.project.run import _PipelineRun
from pipeline.runtime import LoopStep, PhaseStep, PipelineState, Profile, ProfileKind
from pipeline.runtime.resume import LoopResumeBlockedError


def _profile() -> Profile:
    return Profile(
        name="small_task",
        kind=ProfileKind.FULL_CYCLE,
        variant="lite",
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(phase="validate_plan"),
                ),
                until="validate_plan.approved",
                max_rounds=2,
                round_extras_key="plan_round",
            ),
            PhaseStep(phase="implement"),
        ),
    )


def test_legacy_plan_checkpoint_migrates_to_validate_same_round() -> None:
    resolution = resolve_loop_resume(
        _profile(),
        completed_phases=frozenset({"plan"}),
        phase_records=(
            PhaseCheckpointRecord(
                phase="plan",
                data=[{"attempt": 1, "output": "durable plan"}],
            ),
        ),
        cursor_records=(),
    )

    cursor = resolution.cursors[0]
    assert cursor.round_n == 1
    assert cursor.completed_phases == ("plan",)
    assert cursor.next_phase == "validate_plan"
    assert cursor.source == "legacy_checkpoint_migration"
    assert resolution.migrated == (
        LoopCursorRecord(
            loop_key="plan_round",
            loop_phases=("plan", "validate_plan"),
            round_n=1,
            completed_phase="plan",
            next_phase="validate_plan",
        ),
    )


def test_explicit_cursor_wins_over_completed_names_from_prior_round() -> None:
    resolution = resolve_loop_resume(
        _profile(),
        completed_phases=frozenset({"plan", "validate_plan"}),
        phase_records=(),
        cursor_records=(
            LoopCursorRecord(
                loop_key="plan_round",
                loop_phases=("plan", "validate_plan"),
                round_n=1,
                completed_phase="plan",
                next_phase="validate_plan",
            ),
            LoopCursorRecord(
                loop_key="plan_round",
                loop_phases=("plan", "validate_plan"),
                round_n=1,
                completed_phase="validate_plan",
                next_phase=None,
            ),
            LoopCursorRecord(
                loop_key="plan_round",
                loop_phases=("plan", "validate_plan"),
                round_n=2,
                completed_phase="plan",
                next_phase="validate_plan",
            ),
        ),
    )

    assert resolution.cursors[0].round_n == 2
    assert resolution.cursors[0].next_phase == "validate_plan"


def test_non_prefix_legacy_checkpoint_is_typed_blocked() -> None:
    with pytest.raises(LoopResumeBlockedError, match="ordered prefix"):
        resolve_loop_resume(
            _profile(),
            completed_phases=frozenset({"validate_plan"}),
            phase_records=(
                PhaseCheckpointRecord(
                    phase="validate_plan",
                    data=[{"attempt": 1, "approved": False}],
                ),
            ),
            cursor_records=(),
        )


def test_changed_profile_shape_is_typed_blocked() -> None:
    with pytest.raises(LoopResumeBlockedError, match="no longer matches"):
        resolve_loop_resume(
            _profile(),
            completed_phases=frozenset({"plan"}),
            phase_records=(),
            cursor_records=(
                LoopCursorRecord(
                    loop_key="plan_round",
                    loop_phases=("plan", "other_validator"),
                    round_n=1,
                    completed_phase="plan",
                    next_phase="other_validator",
                ),
            ),
        )


def test_preflight_blocks_missing_required_plan_artifact(tmp_path) -> None:
    run_id = "missing-plan"
    store = CheckpointStore(tmp_path / "checkpoints.db", run_id=run_id)
    store.save_config({"profile": "small_task"})
    store.save_phase("plan", [{"attempt": 1, "output": "plan"}])
    store.close()

    with pytest.raises(LoopResumeBlockedError, match="parsed_plan.json"):
        inspect_checkpoint_resume(
            _profile(),
            run_dir=tmp_path,
            run_id=run_id,
        )


def test_run_checkpoint_callback_persists_active_loop_boundary() -> None:
    from types import SimpleNamespace

    store = CheckpointStore(":memory:", run_id="callback")
    run = SimpleNamespace(
        _ckpt=store,
        session={"phases": {"plan": [{"attempt": 1, "output": "plan"}]}},
    )
    state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
    state.extras.update({
        "_active_loop_round_key": "plan_round",
        "_active_loop_phases": ("plan", "validate_plan"),
        "plan_round": 1,
    })

    _PipelineRun._fsm_checkpoint(run, "plan", state)

    assert store.get_loop_cursors() == (
        LoopCursorRecord(
            loop_key="plan_round",
            loop_phases=("plan", "validate_plan"),
            round_n=1,
            completed_phase="plan",
            next_phase="validate_plan",
        ),
    )
