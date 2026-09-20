# SPDX-License-Identifier: Apache-2.0
"""T7 — pre-router resume setup folded into the env-retry boundary.

A ``retry_verification`` resume exists to re-measure a blocking gate set whose
own evidence the env-retry owner must weigh. Everything that runs *before* that
owner is reached must therefore leave the run decidable: the authoritative
ledger initialization in ``build_pipeline_state`` may not refuse the run on a
ledger the owner is about to refuse *properly* — with a named reason, a
re-parked pause, and zero gates executed.

These tests pin the two halves of that boundary:

* the marker path — a broken / absent ledger under ``env_retry_resume=True``
  records :data:`ENV_RETRY_LEDGER_BLOCKED_KEY` instead of raising, and still
  projects the contract so the owner has something to reason about;
* the unchanged path — without the flag every ledger error is raised exactly
  as before, so a non-env-retry resume keeps failing closed at setup.

Detection itself is pinned here too, because ``session_run`` decides it once
and feeds both the header and the state from that single answer: a wrong
answer silently changes which of the two behaviours above the run gets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.protocols import SessionMode
from pipeline.plugins import PluginConfig
from pipeline.project.state_setup import StateInputs, build_pipeline_state
from pipeline.project.types import PresentationPolicy
from pipeline.project.verification_env_retry import (
    ENV_RETRY_LEDGER_BLOCKED_KEY,
    detect_env_retry_resume,
)
from pipeline.project.verification_ledger_runtime import (
    ResumeVerificationLedgerError,
)
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger_store import FILENAME, LedgerStoreError


def _contract_plugin() -> PluginConfig:
    return PluginConfig(
        work_mode="governed",
        verification_envs={"ci": {"image": "python:3.12"}},
        verification={
            "default_env": "ci",
            "commands": {"lint": {"run": "ruff check .", "env": "ci"}},
            "schedule": [
                {"after_phase": "implement", "policy": "require",
                 "commands": ["lint"], "on_fail": "handoff"},
            ],
        },
    )


def _state_inputs(tmp_path: Path, **overrides) -> StateInputs:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    base: dict = {
        "task": "re-measure the failed gate",
        "project_path": tmp_path,
        "plugin": PluginConfig(),
        "phase_config": None,
        "agent_registry": None,
        "output_dir": run_dir,
        "dry_run": True,
        "session": {},
        "session_ts": "20260917_000000",
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
        "verification_contract": VerificationContract.from_plugin(
            _contract_plugin(),
        ),
        "resume_requested": True,
    }
    base.update(overrides)
    return StateInputs(**base)


def _corrupt_ledger(run_dir: Path) -> None:
    (run_dir / FILENAME).write_text("{not json at all", encoding="utf-8")


class TestEnvRetryLedgerInitialization:
    """``env_retry_resume`` turns a setup refusal into a recorded reason."""

    def test_corrupt_ledger_records_the_marker_instead_of_raising(
        self, tmp_path: Path,
    ) -> None:
        inputs = _state_inputs(tmp_path, env_retry_resume=True)
        _corrupt_ledger(inputs.output_dir)

        state = build_pipeline_state(inputs).state

        marker = state.extras[ENV_RETRY_LEDGER_BLOCKED_KEY]
        assert LedgerStoreError.__name__ in str(marker)
        # The owner still needs the contract to judge the decision it was
        # handed, so the projection is NOT skipped along with the ledger.
        assert state.extras["verification_contract"] is inputs.verification_contract
        assert "verification_placeholders" in state.extras

    def test_absent_ledger_records_the_resume_refusal_reason(
        self, tmp_path: Path,
    ) -> None:
        """No ledger at all is the resume refusal, recorded not raised."""
        inputs = _state_inputs(tmp_path, env_retry_resume=True)
        assert not (inputs.output_dir / FILENAME).exists()

        state = build_pipeline_state(inputs).state

        marker = state.extras[ENV_RETRY_LEDGER_BLOCKED_KEY]
        assert ResumeVerificationLedgerError.__name__ in str(marker)

    def test_intact_ledger_leaves_no_marker(self, tmp_path: Path) -> None:
        """A healthy env retry is byte-identical to an ordinary resume."""
        seed = _state_inputs(tmp_path, resume_requested=False)
        build_pipeline_state(seed)  # writes the declaration snapshot
        assert (seed.output_dir / FILENAME).exists()

        inputs = _state_inputs(
            tmp_path, env_retry_resume=True, output_dir=seed.output_dir,
        )
        state = build_pipeline_state(inputs).state

        assert ENV_RETRY_LEDGER_BLOCKED_KEY not in state.extras

    def test_no_contract_never_reaches_the_ledger_at_all(
        self, tmp_path: Path,
    ) -> None:
        inputs = _state_inputs(
            tmp_path, env_retry_resume=True, verification_contract=None,
        )
        _corrupt_ledger(inputs.output_dir)

        state = build_pipeline_state(inputs).state

        assert ENV_RETRY_LEDGER_BLOCKED_KEY not in state.extras


class TestNonEnvRetryKeepsRaising:
    """Without the flag, a bad ledger still refuses the run at setup."""

    def test_corrupt_ledger_raises_ledger_store_error(
        self, tmp_path: Path,
    ) -> None:
        inputs = _state_inputs(tmp_path)  # env_retry_resume defaults to False
        _corrupt_ledger(inputs.output_dir)

        with pytest.raises(LedgerStoreError):
            build_pipeline_state(inputs)

    def test_absent_ledger_raises_the_resume_refusal(
        self, tmp_path: Path,
    ) -> None:
        inputs = _state_inputs(tmp_path)

        with pytest.raises(ResumeVerificationLedgerError):
            build_pipeline_state(inputs)


class TestEnvRetryDetection:
    """The single answer ``session_run`` computes before the header."""

    @staticmethod
    def _decide(run_dir: Path, *, handoff_id: str, action: str) -> None:
        decisions = run_dir / "phase_handoff_decisions"
        decisions.mkdir(parents=True, exist_ok=True)
        (decisions / f"{handoff_id.replace(':', '_')}.json").write_text(
            json.dumps({"handoff_id": handoff_id, "action": action}),
            encoding="utf-8",
        )

    @staticmethod
    def _payload(
        handoff_id: str = "gate:lint:1",
        trigger: str = "verification_gate_failed",
    ) -> dict:
        return {"phase_handoff": {"id": handoff_id, "trigger": trigger}}

    def test_true_only_for_a_matching_pause_and_decision(
        self, tmp_path: Path,
    ) -> None:
        self._decide(tmp_path, handoff_id="gate:lint:1", action="retry_verification")

        assert detect_env_retry_resume(self._payload(), tmp_path) is True
        # Same answer whether the caller holds the prior meta.json or the live
        # session dict — both carry ``phase_handoff`` in one shape.
        assert detect_env_retry_resume(
            {"status": "awaiting_phase_handoff", **self._payload()}, tmp_path,
        ) is True

    def test_false_for_another_id_action_or_trigger(
        self, tmp_path: Path,
    ) -> None:
        self._decide(tmp_path, handoff_id="gate:lint:1", action="retry_verification")
        self._decide(tmp_path, handoff_id="gate:tests:1", action="continue")

        # A decision recorded for some other handoff says nothing about this
        # pause, ...
        assert detect_env_retry_resume(self._payload("gate:tests:1"), tmp_path) is False
        # ... a different trigger is not a verification pause at all, ...
        assert detect_env_retry_resume(
            self._payload(trigger="rejected"), tmp_path,
        ) is False
        # ... and a pause with no recorded retry is an ordinary pause.
        assert detect_env_retry_resume(self._payload("gate:other:1"), tmp_path) is False

    def test_false_without_a_pause_or_a_run_dir(self, tmp_path: Path) -> None:
        self._decide(tmp_path, handoff_id="gate:lint:1", action="retry_verification")

        assert detect_env_retry_resume({}, tmp_path) is False
        assert detect_env_retry_resume(self._payload(), None) is False
        assert detect_env_retry_resume(self._payload(), tmp_path / "gone") is False
