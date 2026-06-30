# SPDX-License-Identifier: Apache-2.0
"""T3 — mono run seeds the run-scoped ParticipantSet onto ``state.extras``.

The single primary participant's ``editable_checkout`` is the resolved run
checkout (the isolated per-run worktree under isolation; the project path in
degraded isolation-off) and ``delivery_target`` the canonical project. The set is
in-memory on ``state.extras`` and never persisted; single-checkout resolution
stays byte-identical.
"""
from __future__ import annotations

from pathlib import Path

from agents.protocols import SessionMode
from pipeline.participants import ParticipantSet
from pipeline.plugins import PluginConfig
from pipeline.project.state_setup import (
    PARTICIPANT_SET_EXTRAS_KEY,
    StateInputs,
    build_pipeline_state,
)
from pipeline.project.types import PresentationPolicy


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
        "session_ts": "20260629_000000",
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


def test_mono_seeds_single_participant_isolated(tmp_path: Path) -> None:
    """Isolated run: editable_checkout = worktree, delivery_target = project."""
    project = tmp_path / "proj"
    worktree = tmp_path / "wt" / "checkout"
    inputs = _state_inputs(
        tmp_path,
        project_path=project,
        git_cwd=str(worktree),
        session={"worktree": {"isolation": "per_run", "base_ref": "abc123"}},
    )

    state = build_pipeline_state(inputs).state

    pset = state.extras[PARTICIPANT_SET_EXTRAS_KEY]
    assert isinstance(pset, ParticipantSet)
    assert len(pset) == 1
    participant = next(iter(pset))
    assert participant.editable_checkout == str(worktree)
    assert participant.delivery_target == str(project)
    assert participant.base_ref == "abc123"
    # Symmetric isolation: the editable checkout is the isolated worktree, and the
    # derived isolated source binds to it (the only verify/edit root).
    src = pset.isolated_source_for(participant)
    assert src is not None and src.is_isolated
    assert src.worktree_path == str(worktree)


def test_mono_set_not_persisted_in_session(tmp_path: Path) -> None:
    """The set is in-memory on state.extras — never written into the session."""
    inputs = _state_inputs(tmp_path, session={"worktree": {"isolation": "off"}})
    state = build_pipeline_state(inputs).state
    assert PARTICIPANT_SET_EXTRAS_KEY in state.extras
    assert PARTICIPANT_SET_EXTRAS_KEY not in inputs.session


def test_mono_degraded_isolation_off(tmp_path: Path) -> None:
    """Degraded isolation-off: checkout == project → editable == delivery, no src."""
    inputs = _state_inputs(
        tmp_path,
        project_path=tmp_path,
        git_cwd=str(tmp_path),
        session={"worktree": {"isolation": "off"}},
    )
    state = build_pipeline_state(inputs).state
    pset = state.extras[PARTICIPANT_SET_EXTRAS_KEY]
    participant = next(iter(pset))
    assert participant.editable_checkout == participant.delivery_target == str(tmp_path)
    assert pset.isolated_source_for(participant) is None


def test_mono_single_checkout_parity(tmp_path: Path) -> None:
    """Single-checkout (git_cwd == project) derives no isolated source — parity."""
    inputs = _state_inputs(tmp_path, project_path=tmp_path, git_cwd=str(tmp_path))
    state = build_pipeline_state(inputs).state
    pset = state.extras[PARTICIPANT_SET_EXTRAS_KEY]
    assert pset.isolated_source_for(next(iter(pset))) is None


def _verification_contract():
    from pipeline.verification_contract import VerificationContract

    return VerificationContract(
        dependency_repos={},
        verification_envs={},
        commands={},
        schedule=(),
        default_env="",
        required=(),
        work_mode="",
    )


def test_verification_placeholders_read_run_scoped_set(tmp_path: Path) -> None:
    """The declared contract's PlaceholderContext derives its isolated_source from
    the SAME run-scoped ParticipantSet seeded onto state.extras (review F1) — the
    set is the single source of truth, not an independently rebuilt one."""
    project = tmp_path / "proj"
    worktree = tmp_path / "wt" / "checkout"
    inputs = _state_inputs(
        tmp_path,
        project_path=project,
        git_cwd=str(worktree),
        session={"worktree": {"isolation": "per_run", "base_ref": "abc123"}},
        verification_contract=_verification_contract(),
    )

    state = build_pipeline_state(inputs).state

    pset = state.extras[PARTICIPANT_SET_EXTRAS_KEY]
    placeholders = state.extras["verification_placeholders"]
    # The placeholder's isolated_source is exactly what the run-scoped set derives
    # for its primary participant — they cannot diverge.
    assert (
        placeholders.isolated_source
        == pset.isolated_source_for(next(iter(pset)))
    )
    assert placeholders.isolated_source is not None
    assert placeholders.isolated_source.worktree_path == str(worktree)


def test_verification_placeholders_single_checkout_parity(tmp_path: Path) -> None:
    """Single-checkout contract run: placeholder isolated_source stays None."""
    inputs = _state_inputs(
        tmp_path,
        project_path=tmp_path,
        git_cwd=str(tmp_path),
        verification_contract=_verification_contract(),
    )
    state = build_pipeline_state(inputs).state
    assert state.extras["verification_placeholders"].isolated_source is None
