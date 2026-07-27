"""Hermetic onboarding proof for scheduled verification cost presentation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger_store import load_ledger
from pipeline.verification_selection import SelectionContext, build_scheduled_gate_plan
from tests.acceptance.test_full_mock_flow import _AlwaysApproved, _init_git_repo


class _RecordingAgent:
    def __init__(self, agent, prompts: list[str]) -> None:
        self._agent = agent
        self._prompts = prompts

    def invoke(self, prompt: str, *args, **kwargs):
        self._prompts.append(prompt)
        return self._agent.invoke(prompt, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._agent, name)


def _plugin(diagnostic_cost: str, proof_cost: str) -> PluginConfig:
    return PluginConfig(
        work_mode="pro",
        verification={
            "commands": {
                "diagnostic": {
                    "run": ["python", "-c", "print('official-fast-diagnostic')"],
                    "cost": diagnostic_cost,
                },
                "proof": {
                    "run": ["python", "-c", "print('official-slow-proof')"],
                    "cost": proof_cost,
                },
            },
            "required": ["proof"],
            "gate_sets": {
                "diagnostics": {"commands": ["diagnostic"]},
                "proofs": {"commands": ["proof"]},
            },
            "selection": [{"always": ["diagnostics", "proofs"]}],
            "schedule": [
                {"after_phase": "implement", "policy": "warn", "commands": ["diagnostic"]},
                {"after_phase": "implement", "policy": "require", "commands": ["proof"]},
            ],
        },
    )


def _run_journey(tmp_path: Path, *, diagnostic_cost: str, proof_cost: str):
    project = tmp_path / "project"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / f"{diagnostic_cost}-{proof_cost}"
    run_dir.mkdir(parents=True)
    prompts: list[str] = []
    provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    provider.codex = lambda _model, **_kwargs: _AlwaysApproved()
    resolve = provider.resolve
    provider.resolve = lambda *args, **kwargs: _RecordingAgent(resolve(*args, **kwargs), prompts)
    plugin = _plugin(diagnostic_cost, proof_cost)
    with patch("pipeline.project.session_run.load_plugin", return_value=plugin):
        session = run_pipeline(
            task="Add a bounded mock change",
            project_dir=str(project),
            output_dir=run_dir,
            max_rounds=1,
            profile_name="feature",
            provider=provider,
        )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return session, run_dir, project, contract, prompts


def _axes(ledger) -> dict[tuple[str, str, str], tuple[object, ...]]:
    return {
        row.identity: (
            row.selected, row.executor, row.execution_policy, row.consequence,
            row.disposition, row.receipt_evidence,
        )
        for row in ledger.rows
    }


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_mock_cost_onboarding_journey_preserves_execution_axes(tmp_path: Path) -> None:
    fast = _run_journey(tmp_path / "fast", diagnostic_cost="fast", proof_cost="slow")
    changed = _run_journey(tmp_path / "changed", diagnostic_cost="moderate", proof_cost="unknown")

    for session, run_dir, _project, _contract, prompts in (fast, changed):
        assert session["status"] == "done"
        assert any("cost=" in prompt for prompt in prompts)
        assert any("engine-owned" in prompt for prompt in prompts)

        ledger = load_ledger(run_dir)
        assert ledger.finalized is True
        assert {row.identity for row in ledger.rows} == {
            ("diagnostic", "after_phase", "implement"),
            ("proof", "after_phase", "implement"),
        }
        assert all(row.executor == "engine" for row in ledger.rows)
        assert all(row.disposition == "executed_pass" for row in ledger.rows)
        assert all(row.receipt_evidence for row in ledger.rows)

        receipts = run_dir / "verification_command_receipts"
        diagnostic = json.loads((receipts / "diagnostic.json").read_text(encoding="utf-8"))
        proof = json.loads((receipts / "proof.json").read_text(encoding="utf-8"))
        assert diagnostic["exit_code"] == proof["exit_code"] == 0
        assert diagnostic["command"] == "diagnostic"
        assert proof["command"] == "proof"

        delivery_readiness = session["phase_log"]["final_acceptance"][
            "verification_autorun"
        ]
        assert "proof" in delivery_readiness["skipped_fresh"]
        assert not delivery_readiness["failed"]

        plan_text = session["phases"]["plan"][0]["output"]
        assert "Commands to run:" in plan_text and "Done Criteria:" in plan_text
        assert "official-fast-diagnostic" not in plan_text
        assert "official-slow-proof" not in plan_text

    fast_ledger = load_ledger(fast[1])
    changed_ledger = load_ledger(changed[1])
    assert _axes(fast_ledger) == _axes(changed_ledger)
    assert [row.cost for row in fast_ledger.rows] == ["fast", "slow"]
    assert [row.cost for row in changed_ledger.rows] == ["moderate", "unknown"]

    fast_actions = {
        (entry.command, entry.hook, entry.phase): entry.action
        for entry in build_scheduled_gate_plan(fast[3], SelectionContext()).entries
    }
    changed_actions = {
        (entry.command, entry.hook, entry.phase): entry.action
        for entry in build_scheduled_gate_plan(changed[3], SelectionContext()).entries
    }
    assert fast_actions == changed_actions

    fast_prompt_text = "\n".join(fast[4])
    changed_prompt_text = "\n".join(changed[4])
    assert "cost=fast" in fast_prompt_text and "cost=slow" in fast_prompt_text
    assert "cost=moderate" in changed_prompt_text and "cost=unknown" in changed_prompt_text
