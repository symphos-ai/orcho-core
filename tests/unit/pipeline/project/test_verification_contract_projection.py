"""T5 — read-only verification-contract projection into header/state.extras.

These tests cover the single, unconditional projection seam:
``project_verification_contract`` (the validation point the coordinator calls
between ``load_plugin`` and the header) and ``build_pipeline_state`` (where the
validated contract + a resolved PlaceholderContext land in ``state.extras``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.entities import SubTask
from agents.protocols import SessionMode
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.project.run_setup import project_verification_contract
from pipeline.project.state_setup import StateInputs, build_pipeline_state
from pipeline.project.types import PresentationPolicy
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
    VerificationContractError,
)


def _contract_plugin() -> PluginConfig:
    return PluginConfig(
        work_mode="governed",
        verification_envs={"ci": {"image": "python:3.12"}},
        dependency_repos={"shared": {"path": "../shared"}},
        verification={
            "default_env": "ci",
            "commands": {"lint": {"run": "ruff check .", "env": "ci"}},
            "schedule": [
                {"after_phase": "implement",
                 "policy": "warn", "commands": ["lint"]},
            ],
        },
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
        "session_ts": "20260609_000000",
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


def _plan(task_id: str = "T1") -> ParsedPlan:
    return ParsedPlan(
        short_summary="plan",
        planning_context="context",
        subtasks=(
            SubTask(id=task_id, goal=f"Do {task_id}", spec="spec"),
        ),
        source="test",
    )


class TestProjectVerificationContractSeam:
    def test_returns_none_without_contract(self) -> None:
        assert project_verification_contract(PluginConfig()) is None

    def test_declared_invalid_contract_raises(self) -> None:
        plugin = PluginConfig(work_mode="turbo")  # not a known work mode
        with pytest.raises(VerificationContractError):
            project_verification_contract(plugin)

    def test_declared_valid_contract_returned(self) -> None:
        contract = project_verification_contract(_contract_plugin())
        assert isinstance(contract, VerificationContract)
        assert contract.work_mode == "governed"


class TestStateExtrasProjection:
    def test_declared_contract_lands_in_state_extras(self, tmp_path: Path) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        inputs = _state_inputs(tmp_path, verification_contract=contract)

        state = build_pipeline_state(inputs).state

        assert state.extras["verification_contract"] is contract
        ph = state.extras["verification_placeholders"]
        assert isinstance(ph, PlaceholderContext)
        assert ph.checkout == str(tmp_path)
        assert ph.project == str(tmp_path)
        assert ph.run_dir == str(tmp_path / "run")
        # dependency path resolved relative to the project path.
        assert ph.dependencies["shared"] == str(tmp_path / ".." / "shared")

    def test_checkout_placeholder_is_worktree_git_cwd(self, tmp_path: Path) -> None:
        """ADR 0090: gate commands verify the run worktree, not the project.

        ``{checkout}`` must resolve to ``inputs.git_cwd`` (the isolated
        worktree checkout) while ``{project}`` stays the original project
        path — the silent-skip incident ran every required dotnet gate
        against the pristine original repo and vacuously passed.
        """
        contract = VerificationContract.from_plugin(_contract_plugin())
        worktree = tmp_path / "worktrees" / "wt_x" / "checkout"
        inputs = _state_inputs(
            tmp_path, git_cwd=str(worktree), verification_contract=contract,
        )

        ph = build_pipeline_state(inputs).state.extras["verification_placeholders"]

        assert ph.checkout == str(worktree)
        assert ph.project == str(tmp_path)

    def test_empty_git_cwd_falls_back_to_project_path(self, tmp_path: Path) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        inputs = _state_inputs(
            tmp_path, git_cwd="", verification_contract=contract,
        )

        ph = build_pipeline_state(inputs).state.extras["verification_placeholders"]

        assert ph.checkout == str(tmp_path)

    def test_no_contract_adds_no_extras_keys(self, tmp_path: Path) -> None:
        inputs = _state_inputs(tmp_path)  # verification_contract defaults to None

        state = build_pipeline_state(inputs).state

        assert "verification_contract" not in state.extras
        assert "verification_placeholders" not in state.extras

    def test_run_dir_none_keeps_placeholder_run_dir_none(self, tmp_path: Path) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        inputs = _state_inputs(
            tmp_path, output_dir=None, verification_contract=contract,
        )

        state = build_pipeline_state(inputs).state

        assert state.extras["verification_placeholders"].run_dir is None


class TestSelectionIntentReachesRoutingViaRunSetup:
    """Production path: a contract-declared ``task_kind`` / ``operator_sets``
    flows through ``build_pipeline_state`` into ``state.extras`` and is consumed
    by the executable gate routing — no manual extras injection."""

    @staticmethod
    def _intent_contract() -> VerificationContract:
        contract = VerificationContract.from_plugin(
            PluginConfig(
                work_mode="governed",
                verification={
                    "commands": {"bug": {"run": "pytest bug"}},
                    "required": ["bug"],
                    "gate_sets": {"bugfix": {"commands": ["bug"]}},
                    "selection": [{"task_kind": "bugfix", "include": ["bugfix"]}],
                    "schedule": [{"after_phase": "implement", "commands": ["bug"]}],
                    "task_kind": "bugfix",
                },
            ),
        )
        assert contract is not None
        return contract

    def test_declared_task_kind_selects_gate_in_routing(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from types import SimpleNamespace

        from pipeline.project import gate_repair

        contract = self._intent_contract()
        inputs = _state_inputs(tmp_path, verification_contract=contract)
        state = build_pipeline_state(inputs).state
        # Sanity: run setup put the contract on state.extras (the wire into
        # routing); no verification_task_kind extras were injected.
        assert state.extras["verification_contract"] is contract
        assert "verification_task_kind" not in state.extras

        run = SimpleNamespace(state=state, session={}, max_rounds=1)
        monkeypatch.setattr(
            gate_repair, "_run_gate_command", lambda *a, **k: {"exit_code": 1},
        )
        monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: None)

        outcome = gate_repair.run_gate_hook(
            run, object(), object(), hook="after_phase", phase="implement",
        )
        # the bugfix gate was selected purely from the contract-declared
        # task_kind threaded through run setup.
        assert outcome.active is True


class TestStateParsedPlanHydration:
    def test_checkpoint_resume_hydrates_parsed_plan_artifact(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _plan("T5"), attempt=1)
        inputs = _state_inputs(tmp_path, output_dir=run_dir)

        state = build_pipeline_state(inputs).state

        assert state.parsed_plan.subtasks[0].id == "T5"
        assert state.plan_markdown
        assert state.extras["resume_artifacts"]["parsed_plan"] == {
            "source": "artifact",
        }

    def test_checkpoint_resume_keeps_explicit_from_run_plan(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _plan("artifact"), attempt=1)
        inputs = _state_inputs(
            tmp_path,
            output_dir=run_dir,
            from_run_plan_loaded=_plan("explicit"),
            from_run_plan_parent_dir=tmp_path / "parent",
        )

        state = build_pipeline_state(inputs).state

        assert state.parsed_plan.subtasks[0].id == "explicit"
        assert state.extras["plan_source_run_id"] == "parent"
        assert "resume_artifacts" not in state.extras

    def test_missing_parsed_plan_artifact_is_noop(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        inputs = _state_inputs(tmp_path, output_dir=run_dir)

        state = build_pipeline_state(inputs).state

        assert getattr(state, "parsed_plan", None) is None
        assert "resume_artifacts" not in state.extras
