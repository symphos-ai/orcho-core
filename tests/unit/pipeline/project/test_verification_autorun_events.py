"""The required-receipt auto-run runs each gate under a visible boundary.

Before a final phase the engine materializes missing / stale required
receipts (ADR 0094). Those commands ran through ``sdk.verify.verify_run``
with no ``gate.start`` / ``gate.end`` boundary and no ``gate.progress``
stream, so a multi-minute suite before ``final_acceptance`` left
``events.jsonl`` silent and the MCP live status on "starting" with no active
gate (dogfood parent ``20260908_131908_4064f0``: seven silent minutes between
``repair_changes`` and ``final_acceptance``). The scheduled after-phase gates
already had both (ADR 0095 / 0190); the auto-run now emits the same pair per
command through the shared ``gate_events`` seam and threads a progress
context into the executor.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.plugins import PluginConfig
from pipeline.project.gate_events import emit_gate_end, emit_gate_start
from pipeline.project.verification_autorun import materialize_required_receipts
from pipeline.verification_contract import VerificationContract

pytestmark = [pytest.mark.project_run]


def _contract(required: list[str]) -> VerificationContract:
    verification: dict[str, Any] = {
        "default_env": "ci",
        "required": list(required),
        "commands": {name: {"run": "true"} for name in required},
        "schedule": [{"before_delivery": True, "policy": "require", "commands": list(required)}],
    }
    contract = VerificationContract.from_plugin(
        PluginConfig(verification_envs={"ci": {}}, verification=verification),
    )
    assert contract is not None
    return contract


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@orcho.invalid"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(argv, cwd=project, check=True)
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    return project, run_dir


class _Sdk:
    """``sdk.verify`` stub recording each ``verify_run`` call and its progress."""

    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.run_calls: list[dict[str, Any]] = []

    def verify_env(self, **kwargs: Any) -> Any:
        return SimpleNamespace(all_passed=True, receipt_path=None)

    def verify_run(self, **kwargs: Any) -> Any:
        """Drive the observer exactly as ``sdk.verify.verify_run`` does."""
        self.run_calls.append(kwargs)
        observer = kwargs["observer"]
        self.progress_by_command: dict[str, Any] = getattr(self, "progress_by_command", {})
        outcomes = []
        for name in kwargs["commands"]:
            self.progress_by_command[name] = observer.start(name)
            outcome = SimpleNamespace(
                command=name, receipt_path=None, exit_code=self.exit_code, duration_s=0.5,
            )
            outcomes.append(outcome)
            observer.end(name, outcome)
        return SimpleNamespace(outcomes=outcomes)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sdk.verify.verify_env", self.verify_env)
        monkeypatch.setattr("sdk.verify.verify_run", self.verify_run)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.observability.events.emit",
        lambda kind, **payload: emitted.append((kind, payload)),
    )
    return emitted


def test_autorun_brackets_each_command_with_a_paired_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_dir = _layout(tmp_path)
    sdk = _Sdk()
    sdk.install(monkeypatch)
    emitted = _capture_events(monkeypatch)

    result = materialize_required_receipts(
        run_id="r1", run_dir=run_dir, project_dir=str(project), checkout=str(project),
        contract=_contract(["unit", "lint"]), ctx=None, reason="pre-final",
        hook="before_phase", phase="final_acceptance",
    )

    assert result.attempted and set(result.ran_commands) == {"unit", "lint"}
    gates = [(k, p) for k, p in emitted if k in ("gate.start", "gate.end")]
    assert [k for k, _ in gates] == ["gate.start", "gate.end", "gate.start", "gate.end"]
    for (_start_kind, start), (_end_kind, end) in zip(gates[::2], gates[1::2], strict=True):
        assert start["name"] == end["name"] in {"unit", "lint"}
        assert start["hook"] == end["hook"] == "before_phase"
        assert start["phase"] == end["phase"] == "final_acceptance"
        assert start["ownership"] == "engine" and start["gate_kind"] == "scheduled"
        assert start["invocation_id"] == end["invocation_id"]
        assert end["outcome"] == "passed"
    assert len({p["invocation_id"] for _, p in gates}) == 2

    # Still ONE batched verify_run call; each command got the progress context
    # of ITS boundary.
    assert len(sdk.run_calls) == 1 and set(sdk.run_calls[0]["commands"]) == {"unit", "lint"}
    for _, start in gates[::2]:
        progress = sdk.progress_by_command[start["name"]]
        assert progress.invocation_id == start["invocation_id"]
        assert progress.name == start["name"]
        assert progress.hook == "before_phase" and progress.phase == "final_acceptance"
    assert [p["duration_s"] for _, p in gates[1::2]] == [0.5, 0.5]


def test_autorun_boundary_reports_a_failed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_dir = _layout(tmp_path)
    _Sdk(exit_code=1).install(monkeypatch)
    emitted = _capture_events(monkeypatch)

    materialize_required_receipts(
        run_id="r1", run_dir=run_dir, project_dir=str(project), checkout=str(project),
        contract=_contract(["unit"]), ctx=None, reason="pre-final",
        hook="before_phase", phase="final_acceptance",
    )

    ends = [p for k, p in emitted if k == "gate.end"]
    assert [e["outcome"] for e in ends] == ["failed"]


def test_autorun_boundary_closes_when_the_executor_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_dir = _layout(tmp_path)
    sdk = _Sdk()
    sdk.install(monkeypatch)

    def boom(**kw: Any) -> Any:
        # The SDK opened the boundary for the first command, then the
        # executor raised before ``end`` — the observer contract says the SDK
        # closes it with ``None``; the materializer also settles any boundary
        # left open.
        kw["observer"].start("unit")
        raise RuntimeError("executor exploded")

    monkeypatch.setattr("sdk.verify.verify_run", boom)
    emitted = _capture_events(monkeypatch)

    result = materialize_required_receipts(
        run_id="r1", run_dir=run_dir, project_dir=str(project), checkout=str(project),
        contract=_contract(["unit"]), ctx=None, reason="pre-final",
        hook="before_phase", phase="final_acceptance",
    )

    assert any("executor exploded" in e for e in result.errors)
    kinds = [k for k, _ in emitted if k in ("gate.start", "gate.end")]
    assert kinds == ["gate.start", "gate.end"]
    assert [p for k, p in emitted if k == "gate.end"][0]["outcome"] == "failed"


def test_autorun_without_targets_emits_no_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_dir = _layout(tmp_path)
    _Sdk().install(monkeypatch)
    emitted = _capture_events(monkeypatch)

    materialize_required_receipts(
        run_id="r1", run_dir=run_dir, project_dir=str(project), checkout=str(project),
        contract=None, ctx=None, reason="pre-final",
        hook="before_phase", phase="final_acceptance",
    )

    assert not [k for k, _ in emitted if k.startswith("gate.")]


def test_gate_events_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _capture_events(monkeypatch)
    emit_gate_start("test", hook="after_phase", phase="implement", invocation_id="abc")
    emit_gate_end(
        "test", hook="after_phase", phase="implement", outcome="passed",
        duration_s=1.0, invocation_id="abc",
        classification=SimpleNamespace(status="present", failure_kind=""),
    )
    assert emitted == [
        ("gate.start", {
            "name": "test", "gate_kind": "scheduled", "command": "test",
            "hook": "after_phase", "phase": "implement", "ownership": "engine",
            "invocation_id": "abc",
        }),
        ("gate.end", {
            "name": "test", "outcome": "passed", "duration_s": 1.0, "command": "test",
            "hook": "after_phase", "phase": "implement", "ownership": "engine",
            "receipt_status": "present", "invocation_id": "abc",
        }),
    ]
