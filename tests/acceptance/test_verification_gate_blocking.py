# SPDX-License-Identifier: Apache-2.0
"""ADR 0090 — a ``require`` verification gate cannot end in a green run.

End-to-end mock proof of the silent-skip incident fix: a project whose
contract schedules ``policy=require`` gates (after_phase implement +
before_delivery) with a broken environment (the gate command cannot
succeed) must NOT complete ``done``/approved — the run pauses at the gate
handoff (``verification_gate_failed``) where the operator can halt, retry,
or ``continue_with_waiver``. The failed receipt is persisted so readiness /
evidence see the same proof routing acted on.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.control.handoff_routing import GateIdentity
from pipeline.plugins import PluginConfig
from pipeline.project.app import run_project_pipeline
from pipeline.project.types import ProjectRunRequest
from pipeline.project.verification_handoff_retry import (
    VerificationHandoffRetryBlocked,
    VerificationHandoffRetryContext,
    apply_verification_handoff_retry,
)
from pipeline.project.verification_ledger_runtime import ResumeVerificationLedgerError
from pipeline.project_orchestrator import run_pipeline
from pipeline.verification_ledger_store import load_ledger
from sdk.phase_handoff import phase_handoff_decide
from sdk.runs import load_meta
from tests.acceptance.test_full_mock_flow import (
    _build_clean_review_provider,
    _init_git_repo,
)

# A command that exists nowhere — models a broken verification env (the
# incident: gates that cannot actually run on this host).
_BROKEN_ARGV = ["orcho-test-definitely-missing-binary"]

GATED_PLUGIN = PluginConfig(
    name="Gated Acceptance Project",
    language="Python",
    work_mode="pro",
    verification={
        "commands": {"gate": {"run": _BROKEN_ARGV}},
        "required": ["gate"],
        "gate_sets": {"required": {"commands": ["gate"]}},
        "selection": [{"always": ["required"]}],
        "schedule": [
            {"after_phase": "implement", "policy": "require",
             "action": "repair_loop", "commands": ["gate"]},
            {"before_delivery": True, "policy": "require",
             "action": "handoff", "commands": ["gate"]},
        ],
    },
)

SMALL_TASK_HANDOFF_PLUGIN = PluginConfig(
    name="Repairless small-task verification",
    language="Python",
    work_mode="pro",
    verification={
        "commands": {"gate": {"run": ["python", "-c", "raise SystemExit(1)"]}},
        "required": ["gate"],
        "gate_sets": {"required": {"commands": ["gate"]}},
        "selection": [{"always": ["required"]}],
        "schedule": [{
            "after_phase": "implement", "policy": "require",
            "action": "handoff", "commands": ["gate"],
        }],
    },
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
class TestRequireGateBlocksGreenRun:
    def _run(self, tmp_path: Path) -> tuple[dict, Path]:
        project = tmp_path / "proj"
        _init_git_repo(project)
        run_dir = tmp_path / "runs" / "20260613_000000"
        run_dir.mkdir(parents=True)
        with patch(
            "pipeline.project.session_run.load_plugin",
            return_value=GATED_PLUGIN,
        ):
            session = run_pipeline(
                task="Add structured logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                profile_name="feature",
                provider=_build_clean_review_provider(),
            )
        return session, run_dir

    def test_broken_required_gate_pauses_run(self, tmp_path: Path) -> None:
        session, run_dir = self._run(tmp_path)

        # The run must NOT be done — it pauses at the gate handoff where
        # only an explicit operator decision (halt / retry / waiver) can
        # move it forward.
        assert session.get("status") == "awaiting_phase_handoff"

        handoff = session.get("phase_handoff") or {}
        assert handoff.get("trigger") == "verification_gate_failed"
        assert "continue_with_waiver" in (
            handoff.get("available_actions") or ()
        )

    def test_failed_gate_receipt_is_persisted(self, tmp_path: Path) -> None:
        _session, run_dir = self._run(tmp_path)

        receipts_dir = run_dir / "verification_command_receipts"
        files = sorted(p.name for p in receipts_dir.glob("*.json"))
        assert files == ["gate.json"]
        receipt = json.loads((receipts_dir / "gate.json").read_text())
        assert receipt["command"] == "gate"
        assert receipt["exit_code"] != 0  # None (spawn failure) or non-zero

    def test_gate_commands_ran_in_worktree_not_project(
        self, tmp_path: Path,
    ) -> None:
        """The receipt's cwd must be the run worktree checkout — the
        incident ran gates against the pristine original project and
        vacuously passed."""
        _session, run_dir = self._run(tmp_path)

        receipt = json.loads(
            (run_dir / "verification_command_receipts" / "gate.json")
            .read_text(),
        )
        project = str(tmp_path / "proj")
        assert receipt["placeholders"]["checkout"] != project
        assert "checkout" in receipt["placeholders"]["checkout"]
        assert receipt["placeholders"]["project"] == project


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_small_task_handoff_omits_unexecutable_retry_feedback(tmp_path: Path) -> None:
    """The real repair-less profile publishes only actions it can execute."""
    project = tmp_path / "proj"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / "20260723_small_task_handoff"
    run_dir.mkdir(parents=True)
    with patch(
        "pipeline.project.session_run.load_plugin",
        return_value=SMALL_TASK_HANDOFF_PLUGIN,
    ):
        session = run_pipeline(
            task="Add structured logging",
            project_dir=str(project),
            output_dir=run_dir,
            max_rounds=1,
            profile_name="small_task",
            provider=_build_clean_review_provider(),
        )

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert session["status"] == meta["status"] == "awaiting_phase_handoff"
    handoff = meta["phase_handoff"]
    assert handoff["trigger"] == "verification_gate_failed"
    assert "retry_feedback" not in handoff["available_actions"]


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_unattended_small_task_gate_halt_rearms_then_completes(tmp_path: Path) -> None:
    """ADR 0154 journey: halt → re-arm → decide → ordinary completion."""
    project = tmp_path / "proj"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / "20260723_unattended_small_task"
    run_dir.mkdir(parents=True)

    def invoke(*, resume_from: str | None = None):
        with patch(
            "pipeline.project.session_run.load_plugin",
            return_value=SMALL_TASK_HANDOFF_PLUGIN,
        ):
            return run_project_pipeline(ProjectRunRequest(
                task="Add structured logging",
                project_dir=str(project),
                output_dir=run_dir,
                resume_from=resume_from,
                max_rounds=1,
                profile_name="small_task",
                provider=_build_clean_review_provider(),
                no_interactive=True,
                unattended=resume_from is None,
            ))

    halted = invoke()
    first_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert halted.session["status"] == first_meta["status"] == "halted"
    assert first_meta["halt_reason"] == "phase_handoff_unattended_halt"
    assert load_meta(run_dir)["status"] == "halted"
    persisted = first_meta["phase_handoff_unattended"]["phase_handoff"]
    assert "retry_feedback" not in persisted["available_actions"]
    assert load_ledger(run_dir).finalized is False

    rearmed = invoke(resume_from=run_dir.name)
    rearmed_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert rearmed.session["status"] == rearmed_meta["status"] == "awaiting_phase_handoff"
    assert rearmed_meta.get("halt_reason") != "interrupted"
    assert rearmed_meta["status"] != "interrupted"
    assert rearmed_meta["phase_handoff"]["available_actions"] == persisted["available_actions"]
    assert load_meta(run_dir)["status"] == "awaiting_phase_handoff"

    phase_handoff_decide(
        run_dir.name,
        rearmed_meta["phase_handoff"]["id"],
        "continue_with_waiver",
        feedback="Accept the failed required gate for this mock journey.",
        runs_dir=run_dir.parent,
        cwd=None,
    )
    completed = invoke(resume_from=run_dir.name)
    final_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert completed.session["status"] == final_meta["status"] == "done"
    assert final_meta.get("halt_reason") != "interrupted"
    assert final_meta["status"] != "interrupted"
    assert load_meta(run_dir)["status"] == "done"
    assert load_ledger(run_dir).finalized is True


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_pre_run_dirty_resume_without_ledger_is_durable_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume ledger refusal retains the pre-run-dirty halt rather than interrupting."""
    project = tmp_path / "proj"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / "20260723_pre_run_dirty"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "task": "Add structured logging",
        "project": str(project),
        "status": "halted",
        "halt_reason": "pre_run_dirty_halt",
    }), encoding="utf-8")
    monkeypatch.setattr(
        "pipeline.project.session_run.setup_checkpoint_and_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ResumeVerificationLedgerError("resume has a verification contract but no scheduled-gate ledger"),
        ),
    )

    with patch(
        "pipeline.project.session_run.load_plugin",
        return_value=SMALL_TASK_HANDOFF_PLUGIN,
    ):
        result = run_project_pipeline(ProjectRunRequest(
            task="Add structured logging",
            project_dir=str(project),
            output_dir=run_dir,
            resume_from=run_dir.name,
            profile_name="small_task",
            provider=_build_clean_review_provider(),
        ))

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert result.session["status"] == meta["status"] == "halted"
    assert meta["halt_reason"] == "pre_run_dirty_halt"
    assert "no scheduled-gate ledger" in meta["resume_refusal"]["message"]
    assert meta["status"] != "interrupted"
    assert meta.get("halt_reason") != "interrupted"


def test_verification_retry_feedback_preserves_human_directed_round_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertical control-flow proof for the operator-visible retry path."""
    from types import SimpleNamespace

    from pipeline.control.handoff_labels import render_round_label

    active = {"id": "gate:pytest-unit:2", "round": 2, "loop_max_rounds": 2}
    run_dir = tmp_path / "runs" / "retry-round-3"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": run_dir.name, "status": "awaiting_phase_handoff"}),
        encoding="utf-8",
    )
    run = SimpleNamespace(
        session={"phase_handoff": active, "status": "awaiting_phase_handoff"},
        state=SimpleNamespace(extras={}, human_feedback="", halt=False, phase_handoff_request=None),
        output_dir=run_dir,
    )
    calls: list[object] = []
    monkeypatch.setattr("pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None)
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    def _repair(_run, *_args, **kwargs) -> None:
        calls.append({"repair": kwargs})
        _run.session["phases"] = {"rounds": [{"round": 3, "critique": "retry"}]}
        (run_dir / "metrics.json").write_text(json.dumps({"phase_attempts": [
            {"phase": "repair_changes", "attempt": 1},
            {"phase": "repair_changes", "attempt": 2},
            {"phase": "repair_changes", "attempt": 3},
        ]}), encoding="utf-8")

    def _rerun(_run, **kwargs) -> bool:
        calls.append(kwargs)
        from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent
        from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger
        row = GateLedgerRow("pytest-unit", "after_phase", "implement", "after_implement", "auto", (), "always", selected=True, execution_policy="require")
        write_ledger(run_dir, ScheduledGateLedger((row,), (
            GateTrailEvent("pytest-unit", "after_phase", "implement", "execution", "fail", receipt_evidence="receipts/original.json"),
            GateTrailEvent("pytest-unit", "after_phase", "implement", "execution", "fail", receipt_evidence="receipts/rerun.json", rerun=True),
        )))
        _run.state.phase_handoff_request = SimpleNamespace(
            handoff_id="gate:pytest-unit:3", round=3, loop_max_rounds=2,
        )
        return False

    monkeypatch.setattr("pipeline.project.verification_handoff_retry._dispatch_one_repair", _repair)
    monkeypatch.setattr("pipeline.project.gate_repair.rerun_verification_handoff_gate", _rerun)

    result = apply_verification_handoff_retry(
        run=run, profile=object(), ctx=object(), active=active,
        handoff_id="gate:pytest-unit:2", feedback="Починить проверку", note=None,
        decided_at="2026-01-01T00:00:00Z",
        identity=GateIdentity("pytest-unit", "after_phase", "implement"),
    )
    assert result.paused is True
    expected = VerificationHandoffRetryContext(
        identity=GateIdentity("pytest-unit", "after_phase", "implement"),
        prior_round=2, fresh_round=3, loop_max_rounds=2,
        human_retry_ordinal=1,
    )
    assert calls[0] == {"repair": {"retry_context": expected}}
    assert calls[1]["retry_context"] == expected
    assert calls[1]["profile"] is not None
    assert render_round_label(
        phase="implement", round=expected.fresh_round,
        loop_max_rounds=expected.loop_max_rounds, human_directed=True,
    ) == "implement human retry 1 after REJECTED verdict"
    assert run.state.human_feedback == "Починить проверку"
    assert run.session["phases"]["rounds"] == [{"round": 3, "critique": "retry"}]
    assert [item["attempt"] for item in json.loads((run_dir / "metrics.json").read_text())["phase_attempts"]] == [1, 2, 3]
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    from sdk.verification_timeline import get_verification_timeline
    rerun = get_verification_timeline(run_id=run_dir.name).events[-1]
    assert (rerun.command, rerun.hook, rerun.phase, rerun.receipt_evidence.path, rerun.receipt_evidence.rerun) == (
        "pytest-unit", "after_phase", "implement", "receipts/rerun.json", True,
    )


def test_retry_control_failure_keeps_subject_but_process_crash_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    def _run() -> SimpleNamespace:
        active = {"id": "gate:pytest-unit:1", "round": 1}
        return SimpleNamespace(
            session={"phase_handoff": active, "status": "awaiting_phase_handoff"},
            state=SimpleNamespace(extras={}, human_feedback="", halt=False, phase_handoff_request=None),
            output_dir=None,
        )

    monkeypatch.setattr("pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None)
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    identity = GateIdentity("pytest-unit", "after_phase", "implement")
    control = _run()
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("bad dispatch")),
    )
    with pytest.raises(VerificationHandoffRetryBlocked, match="bad dispatch"):
        apply_verification_handoff_retry(
            run=control, profile=object(), ctx=object(), active=control.session["phase_handoff"],
            handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
            decided_at="now", identity=identity,
        )
    assert control.session["phase_handoff"]["id"] == "gate:pytest-unit:1"

    crashed = _run()
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(OSError("process crash")),
    )
    with pytest.raises(OSError, match="process crash"):
        apply_verification_handoff_retry(
            run=crashed, profile=object(), ctx=object(), active=crashed.session["phase_handoff"],
            handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
            decided_at="now", identity=identity,
        )
