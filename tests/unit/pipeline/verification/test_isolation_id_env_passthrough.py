"""ADR 0131: project-run isolation ids reach scheduled gate subprocesses."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pipeline.verification_command as verification_command
from pipeline.plugins import PluginConfig
from pipeline.project import gate_repair, session_run
from pipeline.project.types import ProjectRunRequest
from pipeline.verification_contract import PlaceholderContext, VerificationContract
from pipeline.verification_env import RUN_SCOPED_ENV_CHANNELS


class _State:
    def __init__(self, contract: VerificationContract, output_dir: Path) -> None:
        self.extras = {
            "verification_contract": contract,
            "verification_placeholders": PlaceholderContext(
                checkout=str(Path.cwd()), project=str(Path.cwd()),
            ),
        }
        self.output_dir = output_dir
        self.halt = False
        self.halt_reason = ""
        self.phase_handoff_request = None

    def stop(self, reason: str) -> None:
        self.halt = True
        self.halt_reason = reason


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        verification={
            "commands": {"suite": {"run": ["python", "-c", "pass"]}},
            "required": ["suite"],
            "gate_sets": {"core": {"commands": ["suite"]}},
            "selection": [{"always": ["core"]}],
            "schedule": [{"before_delivery": True, "commands": ["suite"]}],
        },
    ))
    assert contract is not None
    return contract


def test_isolation_id_reaches_bootstrap_and_before_delivery_executor(
    monkeypatch, tmp_path: Path,
) -> None:
    """The typed lifecycle, not CLI setup, owns this inherited value."""
    session_ts = "resolved-run-id"
    captured_envs: list[dict[str, str]] = []
    observed: dict[str, object] = {}
    contract = _contract()

    monkeypatch.setenv("ORCHO_ISOLATION_ID", "ambient-id")
    monkeypatch.setattr(
        session_run,
        "_resolve_profile_runtime",
        lambda request: SimpleNamespace(session_ts=session_ts),
    )

    def fake_resolve_state(request, ctx) -> None:
        observed["bootstrap"] = os.environ["ORCHO_ISOLATION_ID"]
        ctx.session = {"status": "running"}
        ctx.halted = False
        run = SimpleNamespace(
            state=_State(contract, tmp_path), session=ctx.session, max_rounds=1,
            _on_phase_start=None, _on_phase_end=None,
        )
        outcome = gate_repair.run_gate_hook(
            run, object(), object(), hook="before_delivery",
        )
        assert outcome.passed
        from pipeline.evidence.verification_receipt import load_command_receipts

        observed["receipt"] = load_command_receipts(tmp_path)[0]

    monkeypatch.setattr(session_run, "_resolve_state", fake_resolve_state)
    monkeypatch.setattr(session_run, "_build_and_dispatch", lambda request, ctx: ctx.session)
    monkeypatch.setattr(
        verification_command,
        "_execute",
        lambda argv, cwd, sub_env: (
            captured_envs.append(dict(sub_env)) or (0, "", "", 0.0, "")
        ),
    )

    request = ProjectRunRequest(
        task="test", project_dir=str(tmp_path), output_dir=tmp_path,
        no_interactive=True,
    )
    session_run.run_project_pipeline_session(request)

    assert observed["bootstrap"] == session_ts
    assert captured_envs[0]["ORCHO_ISOLATION_ID"] == session_ts
    assert observed["receipt"]["env_overrides"] == {}
    assert os.environ["ORCHO_ISOLATION_ID"] == "ambient-id"


def test_isolation_id_is_removed_after_early_halt(monkeypatch, tmp_path: Path) -> None:
    """An early isolation halt restores an absent ambient value."""
    monkeypatch.delenv("ORCHO_ISOLATION_ID", raising=False)
    monkeypatch.setattr(
        session_run,
        "_resolve_profile_runtime",
        lambda request: SimpleNamespace(session_ts="halted-run-id"),
    )

    def fake_resolve_state(request, ctx) -> None:
        assert os.environ["ORCHO_ISOLATION_ID"] == "halted-run-id"
        ctx.session = {"status": "halted"}
        ctx.halted = True

    monkeypatch.setattr(session_run, "_resolve_state", fake_resolve_state)
    request = ProjectRunRequest(
        task="test", project_dir=str(tmp_path), output_dir=tmp_path,
        no_interactive=True,
    )

    session, _, session_ts = session_run.run_project_pipeline_session(request)

    assert session == {"status": "halted"}
    assert session_ts == "halted-run-id"
    assert "ORCHO_ISOLATION_ID" not in os.environ


def test_isolation_id_is_not_stripped_from_gate_env() -> None:
    assert "ORCHO_ISOLATION_ID" not in RUN_SCOPED_ENV_CHANNELS
