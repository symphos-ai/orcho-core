"""Plan-only completion through a durable operator continue and checkpoint resume."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import orcho
from core.infra import config
from pipeline.checkpoint import CheckpointStore
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline
from sdk import collect_evidence, to_jsonable
from sdk.phase_handoff import phase_handoff_decide
from sdk.status import load_status
from tests.acceptance.test_full_mock_flow import (
    _build_clean_review_provider,
    _init_git_repo,
)


@pytest.mark.parametrize("profile", ["planning", "research"])
def test_approved_plan_only_resume(tmp_path: Path, monkeypatch, capsys, profile: str) -> None:
    project = tmp_path / "project"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / "20260917_000000"
    run_dir.mkdir(parents=True)
    plugin = PluginConfig(
        name="Plan-only delivery regression", language="Python",
        verification={
            "commands": {"lint": {"run": ["python", "-c", "raise SystemExit(1)"]}},
            "required": ["lint"],
            "gate_sets": {"required": {"commands": ["lint"]}},
            "selection": [{"always": ["required"]}],
            "schedule": [{"after_phase": "implement", "policy": "require",
                          "action": "handoff", "commands": ["lint"]}],
        },
    )
    monkeypatch.setattr("pipeline.project.session_run.load_plugin", lambda *_a, **_k: plugin)
    app_config = deepcopy(config.AppConfig.load())
    app_config.commit.update(enabled=True, decision_mode="auto")
    monkeypatch.setattr(config.AppConfig, "load", lambda: app_config)
    kwargs = dict(
        task="Plan structured logging", project_dir=str(project), output_dir=run_dir,
        profile_name=profile, provider=_build_clean_review_provider(),
        hypothesis_enabled=False, no_interactive=True,
    )
    paused = run_pipeline(**kwargs)
    assert paused["status"] == "awaiting_phase_handoff"
    assert paused["phase_handoff"]["trigger"] == "approved"
    plan = (run_dir / "parsed_plan.json").read_bytes()
    with CheckpointStore(run_dir / "checkpoints.db") as store:
        checkpoint = store.load()
        history = store.get_phase_records(checkpoint.run_id)
    phase_handoff_decide(
        run_dir.name, paused["phase_handoff"]["id"], "continue",
        runs_dir=run_dir.parent, cwd=None,
    )
    resumed = run_pipeline(**kwargs, resume_from=checkpoint.run_id)
    assert resumed["status"] == "done", resumed.get("halt_reason")
    assert resumed["commit_delivery"]["status"] in {"not_applicable", "no_diff"}
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "done"
    assert meta["commit_delivery"] == resumed["commit_delivery"]
    with CheckpointStore(run_dir / "checkpoints.db") as store:
        assert store.load(checkpoint.run_id).status.value == "done"
        assert store.get_phase_records(checkpoint.run_id)[:len(history)] == history
    status = load_status(run_dir.name, runs_dir=run_dir.parent, cwd=None)
    assert status.meta.status == "done"
    assert (run_dir / "parsed_plan.json").read_bytes() == plan

    assert resumed["phases"]["validate_plan"][-1]["verdict"] == "APPROVED"
    decisions = list((run_dir / "phase_handoff_decisions").glob("*.json"))
    assert decisions and json.loads(decisions[0].read_text())["action"] == "continue"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [e for e in events if e["kind"] == "run.end"][-1]["payload"]["status"] == "done"
    evidence = json.loads((run_dir / "evidence.json").read_text())
    assert evidence["status"] == "done"
    assert not evidence["verification_receipts"]
    assert "persist_no_delivery" not in meta["commit_delivery"]

    bundle = collect_evidence(run_dir.name, runs_dir=run_dir.parent, cwd=None)
    assert bundle.valid, bundle.validation_errors
    assert bundle.body["status"] == "done"
    assert not bundle.body["verification_receipts"]
    payload = json.loads(json.dumps(to_jsonable(status)))
    assert payload["meta"]["status"] == "done"
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    monkeypatch.chdir(project)
    capsys.readouterr()
    assert orcho.cmd_status(SimpleNamespace(
        run_id=run_dir.name, workspace=None, verbose=False, json=True,
    )) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["status"]["meta"]["status"] == "done"
    assert cli_payload["status"]["next_actions"] == payload["next_actions"]

    # C2: SDK и CLI сохраняют единственное готовое продолжение после done.
    action, = status.next_actions
    assert action.tool == "orcho_run_start"
    assert action.kind == "ready_call"
    assert action.args == {
        "from_run_plan": run_dir.name, "profile": "feature", "task": kwargs["task"],
    }
    assert payload["next_actions"] == [to_jsonable(action)]


@pytest.mark.parametrize("profile", ["planning", "research"])
@pytest.mark.parametrize("rejected", [False, True])
def test_plan_handoff_halt_remains_terminal(tmp_path, monkeypatch, profile, rejected):
    from agents.runtimes import MockAgentProvider
    from pipeline.project.bootstrap import PhaseHandoffHaltedError

    project = tmp_path / "project"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / "20260917_010000"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr("pipeline.project.session_run.load_plugin", lambda *_a, **_k: PluginConfig())
    provider = (
        MockAgentProvider(latency=0.0, validate_plan_reject_rounds=99)
        if rejected else _build_clean_review_provider()
    )
    kwargs = dict(
        task="Plan structured logging", project_dir=str(project), output_dir=run_dir,
        profile_name=profile, provider=provider, hypothesis_enabled=False,
        no_interactive=True,
    )
    paused = run_pipeline(**kwargs)
    assert paused["status"] == "awaiting_phase_handoff"
    assert paused["phase_handoff"]["verdict"] == ("REJECTED" if rejected else "APPROVED")
    assert "commit_delivery" not in paused
    with CheckpointStore(run_dir / "checkpoints.db") as store:
        run_id = store.load().run_id
    phase_handoff_decide(
        run_dir.name, paused["phase_handoff"]["id"], "halt",
        note="Stop this plan", runs_dir=run_dir.parent, cwd=None,
    )
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "phase_handoff_halt"
    with pytest.raises(PhaseHandoffHaltedError):
        run_pipeline(**kwargs, resume_from=run_id)
    assert load_status(run_dir.name, runs_dir=run_dir.parent, cwd=None).meta.status == "halted"
