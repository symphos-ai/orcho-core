# SPDX-License-Identifier: Apache-2.0
"""ADR 0090 — a ``require`` verification gate cannot end in a green run.

End-to-end mock proof of the silent-skip incident fix: a project whose
contract schedules ``policy=require`` gates (after_phase implement +
before_delivery) with a broken environment (the gate command cannot
succeed) must NOT complete ``done``/approved — the run pauses at the gate
handoff (``verification_gate_failed``) where the operator can halt, retry,
or ``continue_with_waiver``. The failed receipt is persisted so readiness /
evidence see the same proof routing acted on.

The second half of this module (ADR 0195) drives the ``retry_verification``
journey the same way — through the *public* resume surfaces, with no engine
seam mocked — because the properties under test are positional: which
component reads the ledger first, whether a checkout is materialised at all,
and whether a blocked retry leaves a decidable pause or a dead run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.control.handoff_routing import GateIdentity
from pipeline.engine.worktree import WorktreeConfigError
from pipeline.plugins import PluginConfig
from pipeline.project.app import run_project_pipeline
from pipeline.project.types import PresentationPolicy, ProjectRunRequest
from pipeline.project.verification_handoff_retry import (
    VerificationHandoffRetryBlocked,
    VerificationHandoffRetryContext,
    apply_verification_handoff_retry,
)
from pipeline.project.verification_ledger_runtime import ResumeVerificationLedgerError
from pipeline.project_orchestrator import run_pipeline
from pipeline.verification_ledger_store import load_ledger
from sdk.phase_handoff import (
    PhaseHandoffDecision,
    phase_handoff_decide,
    safe_handoff_id,
)
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

    active = {
        "id": "gate:pytest-unit:2", "round": 2, "loop_max_rounds": 2,
        # The persisted gate critique the retry seam recovers repair inputs from.
        "last_output": "Required verification gate failed.\nCommand: pytest-unit",
    }
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
        identities=(GateIdentity("pytest-unit", "after_phase", "implement"),),
        prior_round=2, fresh_round=3, loop_max_rounds=2,
        human_retry_ordinal=1,
    )
    assert calls[0] == {"repair": {
        "retry_context": expected,
        # The loop this human-directed round belongs to, so a gate failing
        # inside it can record a position a resume can return to. ``None``
        # here: the profile stand-in this scenario drives has no loops.
        "repair_loop": None,
    }}
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
        active = {
            "id": "gate:pytest-unit:1", "round": 1,
            "last_output": "Required verification gate failed.\nCommand: pytest-unit",
        }
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


# ═════════════════════════════════════════════════════════════════════════════
# ADR 0195 — ``retry_verification``: the whole operator journey, unmocked.
#
# One producer for every scenario below: a project whose ``require`` gate set
# is two commands that do not exist on this host. Both fail as ``env_failure``
# (the executor reports no exit code at all), which is the one blocking verdict
# no agent can move — so the pause leads with ``retry_verification``.
#
# From there the operator either repaired the environment (scenario 1), did not
# (scenario 2), or the run's own record can no longer prove what to re-execute
# (scenarios 3a–3h) or where (scenarios 4a–4c). The last two families are the
# load-bearing ones: a blocked env retry must re-park a *decidable* pause having
# executed zero gate commands and materialised zero new checkouts — never crash
# the process, never halt the run, and never silently re-measure a fresh tree.
# ═════════════════════════════════════════════════════════════════════════════

_ENV_GATE_A = "orcho-env-gate-a"
_ENV_GATE_B = "orcho-env-gate-b"

#: Two required commands, scheduled twice each: once ``after_phase(implement)``
#: (the pause under test) and once ``before_delivery``. The second schedule is
#: not decoration — it gives the ledger a *second declared identity for the same
#: command*, which is the only way to prove the retry re-executes by identity
#: rather than by command name (scenario 3e).
ENV_RETRY_PLUGIN = PluginConfig(
    name="Env-retry acceptance project",
    language="Python",
    work_mode="pro",
    verification={
        "commands": {
            _ENV_GATE_A: {"run": [_ENV_GATE_A]},
            _ENV_GATE_B: {"run": [_ENV_GATE_B]},
        },
        "required": [_ENV_GATE_A, _ENV_GATE_B],
        "gate_sets": {"required": {"commands": [_ENV_GATE_A, _ENV_GATE_B]}},
        "selection": [{"always": ["required"]}],
        "schedule": [
            {"after_phase": "implement", "policy": "require",
             "action": "repair_loop", "commands": [_ENV_GATE_A, _ENV_GATE_B]},
            {"before_delivery": True, "policy": "require",
             "action": "repair_loop", "commands": [_ENV_GATE_A, _ENV_GATE_B]},
        ],
    },
)

_ENV_RETRY_TASK = "Add structured logging"


@dataclass(frozen=True)
class _EnvGatePause:
    """The producer's output: a run paused on an env-only required gate set."""

    session: dict
    project: Path
    run_dir: Path
    handoff_id: str
    retained: Path

    @property
    def worktrees_dir(self) -> Path:
        """The runspace ``worktrees/`` root the retained checkout lives under."""
        return self.retained.parent.parent


def _produce_env_gate_pause(tmp_path: Path) -> _EnvGatePause:
    """Run the pipeline to its env-failure gate pause and assert its shape.

    Shared by every scenario, so the preconditions each one then perturbs are
    asserted exactly once, here: the pause is a verification-gate pause, every
    finding is an ``env_failure``, the menu *leads* with ``retry_verification``,
    and the persisted record names both blocking identities with a
    ``receipt_evidence`` pointer on each.
    """
    project = tmp_path / "proj"
    _init_git_repo(project)
    run_dir = tmp_path / "runs" / "20260917_env_retry"
    run_dir.mkdir(parents=True)
    with patch(
        "pipeline.project.session_run.load_plugin", return_value=ENV_RETRY_PLUGIN,
    ):
        session = run_pipeline(
            task=_ENV_RETRY_TASK,
            project_dir=str(project),
            output_dir=run_dir,
            max_rounds=1,
            profile_name="feature",
            provider=_build_clean_review_provider(),
        )

    assert session["status"] == "awaiting_phase_handoff"
    handoff = session["phase_handoff"]
    assert handoff["trigger"] == "verification_gate_failed"
    # Leading, not merely present: repairing the environment is the operator's
    # likeliest next move for a set no agent can reach.
    assert handoff["available_actions"][0] == "retry_verification"
    artifacts = handoff["artifacts"]
    assert [finding["failure_kind"] for finding in artifacts["findings"]] == [
        "env_failure", "env_failure",
    ]
    assert [
        (item["command"], item["hook"], item["phase"])
        for item in artifacts["gate_identities"]
    ] == [
        (_ENV_GATE_A, "after_phase", "implement"),
        (_ENV_GATE_B, "after_phase", "implement"),
    ]
    for item in artifacts["gate_identities"]:
        assert (run_dir / item["receipt_evidence"]).is_file()

    retained = Path(session["worktree"]["path"])
    assert retained.is_dir()
    return _EnvGatePause(
        session=session, project=project, run_dir=run_dir,
        handoff_id=handoff["id"], retained=retained,
    )


def _decide(
    paused: _EnvGatePause, action: str, **kwargs,
) -> PhaseHandoffDecision:
    return phase_handoff_decide(
        paused.run_dir.name,
        kwargs.pop("handoff_id", paused.handoff_id),
        action,
        runs_dir=paused.run_dir.parent,
        cwd=None,
        **kwargs,
    )


def _decide_retry_verification(paused: _EnvGatePause) -> None:
    """Record the operator's ``retry_verification`` — with no feedback at all."""
    decision = _decide(paused, "retry_verification")
    assert decision.action == "retry_verification"
    assert decision.feedback is None
    assert "orcho_run_resume" in {item.tool for item in decision.next_actions}


def _resume_terminal(paused: _EnvGatePause) -> dict:
    """The default public resume surface: ``run_pipeline(resume_from=...)``."""
    with patch(
        "pipeline.project.session_run.load_plugin", return_value=ENV_RETRY_PLUGIN,
    ):
        return run_pipeline(
            task=_ENV_RETRY_TASK,
            project_dir=str(paused.project),
            output_dir=paused.run_dir,
            max_rounds=1,
            profile_name="feature",
            provider=_build_clean_review_provider(),
            resume_from=paused.run_dir.name,
        )


def _resume_silent(paused: _EnvGatePause) -> dict:
    """The headless embedder surface: SILENT + ``no_interactive``."""
    with patch(
        "pipeline.project.session_run.load_plugin", return_value=ENV_RETRY_PLUGIN,
    ):
        return run_project_pipeline(ProjectRunRequest(
            task=_ENV_RETRY_TASK,
            project_dir=str(paused.project),
            output_dir=paused.run_dir,
            max_rounds=1,
            profile_name="feature",
            provider=_build_clean_review_provider(),
            resume_from=paused.run_dir.name,
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )).session


#: The two public resume surfaces, parametrised where a scenario must hold on
#: both. They differ in more than rendering: only TERMINAL prints the run
#: header, and the header is itself a pre-router ledger *reader*.
_RESUMES = {"terminal": _resume_terminal, "silent": _resume_silent}


def _repair_gate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Make both gate commands exist and succeed — the operator's out-of-run fix.

    Nothing in the checkout changes: the commands appear on ``PATH``, which is
    exactly what an ``env_failure`` means and exactly what the engine is asked
    to re-measure.
    """
    bin_dir = tmp_path / "gate_bin"
    bin_dir.mkdir(exist_ok=True)
    for name in (_ENV_GATE_A, _ENV_GATE_B):
        script = bin_dir / name
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


# ── observation helpers (read-only) ─────────────────────────────────────────


def _meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def _write_meta(run_dir: Path, meta: dict) -> None:
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def _ledger_bytes(run_dir: Path) -> bytes | None:
    """The ledger artifact verbatim, or ``None`` when absent.

    Compared byte-for-byte rather than parsed, because several scenarios
    deliberately leave it unparseable — and "zero gate executions" has to be
    provable for exactly those.
    """
    path = run_dir / "scheduled_gate_ledger.json"
    return path.read_bytes() if path.is_file() else None


def _executions(run_dir: Path) -> list[tuple]:
    trail = json.loads(
        (run_dir / "scheduled_gate_ledger.json").read_text(encoding="utf-8"),
    )["trail"]
    return [
        (e["command"], e["hook"], e["phase"], e["outcome"], e["rerun"],
         e["receipt_evidence"])
        for e in trail if e["kind"] == "execution"
    ]


def _worktree_registry(project: Path) -> str:
    """The source repo's whole worktree registry, verbatim."""
    return subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project, capture_output=True, text=True, check=True,
    ).stdout


def _worktree_paths(project: Path) -> list[str]:
    """Only the registered worktree *paths*.

    A completed run legitimately moves the retained checkout onto its delivery
    branch, so the full registry is not stable across a successful resume —
    the set of registered checkouts is, and that is what "no new worktree was
    materialised" actually means.
    """
    return sorted(
        line.split(" ", 1)[1]
        for line in _worktree_registry(project).splitlines()
        if line.startswith("worktree ")
    )


def _worktree_dirs(worktrees_dir: Path) -> list[str]:
    if not worktrees_dir.is_dir():
        return []
    return sorted(p.name for p in worktrees_dir.iterdir())


def _phase_events(run_dir: Path, *, since: int) -> list[dict]:
    return [
        event for event in _read_jsonl(run_dir / "events.jsonl")[since:]
        if event.get("kind") in ("phase.start", "phase.end")
    ]


def _decision_artifacts(run_dir: Path) -> dict[str, str]:
    directory = run_dir / "phase_handoff_decisions"
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.json"))
    }


# ── scenario 1: the environment was repaired, and the run walks on ──────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_reexecutes_the_gate_set_and_completes_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy journey: fix the env, resume, finish — no agent, same run.

    Everything asserted here is about *continuity*: the same run id, the same
    retained checkout, no new worktree, and no phase behind the raising one
    re-entered — not even to announce a skip. The engine re-measures precisely
    the two identities the operator decided on, and each one's trail ends
    ``rerun=True`` + ``pass`` on top of its original ``rerun=False`` + ``fail``;
    the run then walks the review loop and the terminal gate exactly as it
    would have if the gates had passed the first time.
    """
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)

    events_before = len(_read_jsonl(paused.run_dir / "events.jsonl"))
    registry_before = _worktree_paths(paused.project)
    worktrees_before = _worktree_dirs(paused.worktrees_dir)

    _repair_gate_environment(tmp_path, monkeypatch)
    resumed = _resume_terminal(paused)

    meta = _meta(paused.run_dir)
    assert resumed["status"] == meta["status"]
    assert meta["status"] not in ("awaiting_phase_handoff", "interrupted")
    assert meta.get("halt_reason") != "interrupted"
    # Same run: the retry continued the paused run's own directory, event log
    # and ledger rather than starting a second run beside it.
    assert [p.name for p in paused.run_dir.parent.iterdir()] == [
        paused.run_dir.name,
    ]
    assert load_meta(paused.run_dir)["status"] == meta["status"]

    # Nothing behind the gates ran, and no phase that did no work announced
    # itself. A ``phase.start`` for a write phase is exactly what "an agent
    # round is happening here" looks like from outside the run, so neither the
    # skipped ``implement`` (behind the resume point) nor the clean-review
    # ``repair_changes`` (ahead of it, but with nothing to repair) may publish
    # one. The plan round behind ``implement`` is held to the same bar.
    events = _phase_events(paused.run_dir, since=events_before)
    started = [
        event["payload"]["phase_key"] for event in events
        if event["kind"] == "phase.start" and "phase_key" in event["payload"]
    ]
    assert "implement" not in started, started
    assert "repair_changes" not in started, started
    assert "plan" not in started and "validate_plan" not in started, started
    phase_records = {
        (event["payload"]["phase_key"], event["kind"]): event["payload"].get("outcome")
        for event in events if "phase_key" in event["payload"]
    }
    assert ("implement", "phase.end") not in phase_records
    assert ("repair_changes", "phase.end") not in phase_records

    # What *is* ahead of the resume point runs for real: the review loop and
    # the terminal gate, exactly as they would have had the gates passed the
    # first time.
    assert phase_records[("review_changes", "phase.end")] == "ok"
    # Suppressing the trace suppressed nothing else: the clean round still
    # carries its full record, and it records no repair output — this resume
    # re-measured gates, it did not re-open the change.
    round_record = meta["phases"]["rounds"][0]
    assert round_record["review"]["approved"] is True
    assert round_record["critique"] == ""
    assert "prompt_render_review" in round_record
    assert "repair_output" not in round_record

    # And no phase at all started before the gate re-execution finished, which
    # is what separates this arm from ``retry_feedback``'s repair round.
    tail = _read_jsonl(paused.run_dir / "events.jsonl")[events_before:]
    before_gate = tail[:next(
        i for i, event in enumerate(tail) if event["kind"] == "gate.end"
    )]
    assert not [event for event in before_gate if event["kind"] == "phase.start"]

    # Ledger: original failure, then exactly one rerun pass per identity.
    executions = _executions(paused.run_dir)
    for command in (_ENV_GATE_A, _ENV_GATE_B):
        trail = [row for row in executions
                 if row[0] == command and row[1] == "after_phase"]
        assert [(row[3], row[4]) for row in trail] == [
            ("fail", False), ("pass", True),
        ]
        assert (paused.run_dir / trail[-1][5]).is_file()

    # The final receipts are the passing ones, on the same retained subject.
    for command in (_ENV_GATE_A, _ENV_GATE_B):
        receipt = json.loads(
            (paused.run_dir / "verification_command_receipts" / f"{command}.json")
            .read_text(encoding="utf-8"),
        )
        assert receipt["exit_code"] == 0
        assert receipt["placeholders"]["checkout"] == str(paused.retained)

    assert meta["worktree"]["path"] == str(paused.retained)
    assert _worktree_dirs(paused.worktrees_dir) == worktrees_before
    assert _worktree_paths(paused.project) == registry_before


# ── scenario 2: the environment was not repaired — an honest re-pause ───────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_that_fails_again_reparks_a_fresh_retryable_pause(
    tmp_path: Path,
) -> None:
    """A re-execution that fails again is a new pause, not a silent pass.

    The re-park is *retryable* again — the record still proves an env-only set
    — but it carries a fresh handoff id, because the old decision is immutable
    audit evidence whose id must not be reusable.
    """
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)

    resumed = _resume_terminal(paused)

    meta = _meta(paused.run_dir)
    assert resumed["status"] == meta["status"] == "awaiting_phase_handoff"
    handoff = meta["phase_handoff"]
    assert handoff["id"] != paused.handoff_id
    assert handoff["round"] == 2
    assert handoff["trigger"] == "verification_gate_failed"
    assert [finding["failure_kind"] for finding in handoff["artifacts"]["findings"]] == [
        "env_failure", "env_failure",
    ]
    assert handoff["available_actions"][0] == "retry_verification"
    for item in handoff["artifacts"]["gate_identities"]:
        assert (paused.run_dir / item["receipt_evidence"]).is_file()

    # The failed re-execution is recorded as a rerun, not as a first attempt.
    executions = _executions(paused.run_dir)
    for command in (_ENV_GATE_A, _ENV_GATE_B):
        trail = [row for row in executions
                 if row[0] == command and row[1] == "after_phase"]
        assert [(row[3], row[4]) for row in trail] == [
            ("fail", False), ("fail", True),
        ]
    assert meta["worktree"]["path"] == str(paused.retained)


# ── scenarios 3a–3h: every blocker re-parks a decidable pause ───────────────


@dataclass(frozen=True)
class _PreResume:
    """Everything a blocked resume must leave exactly as it found it."""

    ledger: bytes | None
    decisions: dict[str, str]
    worktrees: list[str]
    registry: str


def _snapshot(paused: _EnvGatePause) -> _PreResume:
    return _PreResume(
        ledger=_ledger_bytes(paused.run_dir),
        decisions=_decision_artifacts(paused.run_dir),
        worktrees=_worktree_dirs(paused.worktrees_dir),
        registry=_worktree_registry(paused.project),
    )


def _patch_active_artifacts(run_dir: Path, mutate) -> None:
    """Rewrite the persisted active handoff's artifacts — the retry's record.

    Every 3x scenario damages this record (or what it points at) *after* the
    operator's decision is already on disk, which is exactly the shape a crash,
    a partial write, or a hand-edit leaves behind.
    """
    meta = _meta(run_dir)
    mutate(meta["phase_handoff"]["artifacts"])
    _write_meta(run_dir, meta)


def _assert_reparked_without_executing(
    paused: _EnvGatePause,
    session: dict,
    before: _PreResume,
    *,
    reason_contains: str,
    retry_offered: bool,
) -> None:
    """The one shape every blocked env retry must land in.

    A fresh ``:retry_blocked`` pause carrying the block reason, zero gate
    executions (the ledger artifact is byte-identical), the operator's original
    decision untouched as audit evidence, the retained subject still recorded,
    no new checkout — and a pause the operator can actually decide again.
    """
    meta = _meta(paused.run_dir)
    assert session["status"] == meta["status"] == "awaiting_phase_handoff"
    assert meta["status"] != "interrupted"
    assert meta.get("halt_reason") != "interrupted"

    handoff = meta["phase_handoff"]
    assert handoff["id"] == f"{paused.handoff_id}:retry_blocked"
    assert handoff["trigger"] == "verification_gate_failed"
    reason = handoff["artifacts"]["retry_blocked_reason"]
    assert reason_contains in reason, reason
    actions = handoff["available_actions"]
    assert ("retry_verification" in actions) is retry_offered, actions
    assert "continue_with_waiver" in actions and "halt" in actions

    # Nothing ran and nothing was rebuilt.
    assert _ledger_bytes(paused.run_dir) == before.ledger
    assert _decision_artifacts(paused.run_dir) == before.decisions
    assert meta["worktree"]["path"] == str(paused.retained)
    assert _worktree_dirs(paused.worktrees_dir) == before.worktrees
    assert _worktree_registry(paused.project) == before.registry

    # Still decidable: the re-parked pause is a real handoff, not a tombstone.
    _decide(
        paused, "continue_with_waiver", handoff_id=handoff["id"],
        feedback="Accept the unprovable retry and move on.",
    )
    assert _meta(paused.run_dir)["status"] == "awaiting_phase_handoff"


def _blocked_env_retry(
    tmp_path: Path, sabotage, *, resume=_resume_terminal,
) -> tuple[_EnvGatePause, dict, _PreResume]:
    """Produce → decide → damage the record → resume. Returns what to assert on."""
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)
    sabotage(paused)
    before = _snapshot(paused)
    return paused, resume(paused), before


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_blocked_by_malformed_secondary_identity(tmp_path: Path) -> None:
    """3a — one identity in the set lost its ``hook``: the set is unreadable.

    A partially-parseable identity list is not a smaller gate set; it is a
    record that cannot say what to re-execute. The re-park therefore also
    withdraws the offer, because the same admission test that blocked the
    retry is what publishes the menu.
    """
    def sabotage(paused: _EnvGatePause) -> None:
        _patch_active_artifacts(
            paused.run_dir,
            lambda artifacts: artifacts["gate_identities"][1].pop("hook"),
        )

    paused, session, before = _blocked_env_retry(tmp_path, sabotage)
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="does not prove an env-only retryable set",
        retry_offered=False,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_blocked_by_malformed_primary_identity(tmp_path: Path) -> None:
    """3b — the *primary* identity lost its ``hook``.

    Distinct from 3a: the primary is the handoff's own key, so a reader that
    only validated the list would still find two well-formed members and
    happily re-execute a set whose owner it cannot name.
    """
    def sabotage(paused: _EnvGatePause) -> None:
        _patch_active_artifacts(
            paused.run_dir,
            lambda artifacts: artifacts["gate_identity"].pop("hook"),
        )

    paused, session, before = _blocked_env_retry(tmp_path, sabotage)
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="does not prove an env-only retryable set",
        retry_offered=False,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_env_retry_blocked_by_unparseable_ledger_on_both_surfaces(
    tmp_path: Path, surface: str,
) -> None:
    """3c — a corrupt ledger re-parks; it never kills the process.

    Parametrised over both public surfaces deliberately. Only TERMINAL prints
    the run header, and the header reads the ledger *before* the router — so a
    corrupt ledger reaches a different first reader on each surface, and both
    have to reach the same owner and the same decidable pause. The record
    itself is intact, so the operator is still offered the retry after
    restoring the ledger.
    """
    def sabotage(paused: _EnvGatePause) -> None:
        (paused.run_dir / "scheduled_gate_ledger.json").write_text(
            "{not json at all", encoding="utf-8",
        )

    paused, session, before = _blocked_env_retry(
        tmp_path, sabotage, resume=_RESUMES[surface],
    )
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="scheduled-gate ledger is not trusted for this resume",
        retry_offered=True,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_env_retry_blocked_by_missing_ledger_on_both_surfaces(
    tmp_path: Path, surface: str,
) -> None:
    """3d — the ledger is gone: a pause, not a halt, on both surfaces."""
    def sabotage(paused: _EnvGatePause) -> None:
        (paused.run_dir / "scheduled_gate_ledger.json").unlink()

    paused, session, before = _blocked_env_retry(
        tmp_path, sabotage, resume=_RESUMES[surface],
    )
    assert before.ledger is None
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="scheduled-gate ledger is not trusted for this resume",
        retry_offered=True,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_blocked_when_an_identity_is_swapped_for_its_delivery_twin(
    tmp_path: Path,
) -> None:
    """3e — same command, other scheduled identity: no execution evidence.

    The record stays perfectly well-formed — the commands still agree with the
    findings — so nothing but the ledger can catch it. ``before_delivery`` for
    this command never ran, and a retry that matched by *command name* would
    re-execute a gate whose failure nobody ever observed.
    """
    def sabotage(paused: _EnvGatePause) -> None:
        def mutate(artifacts: dict) -> None:
            artifacts["gate_identities"][1].update(
                {"hook": "before_delivery", "phase": ""},
            )
        _patch_active_artifacts(paused.run_dir, mutate)

    paused, session, before = _blocked_env_retry(tmp_path, sabotage)
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="before_delivery",
        retry_offered=True,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_blocked_when_one_identity_has_no_receipt_evidence(
    tmp_path: Path,
) -> None:
    """3f — partial evidence is not evidence."""
    def sabotage(paused: _EnvGatePause) -> None:
        _patch_active_artifacts(
            paused.run_dir,
            lambda artifacts: artifacts["gate_identities"][1].pop(
                "receipt_evidence",
            ),
        )

    paused, session, before = _blocked_env_retry(tmp_path, sabotage)
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="does not prove an env-only retryable set",
        retry_offered=False,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_blocked_by_a_receipt_that_is_not_in_strict_form(
    tmp_path: Path,
) -> None:
    """3g — ``{}`` is the trap: the tolerant classifier reads it as env_failure.

    A receipt with no exit code classifies as exactly the failure this retry
    looks for, so an owner that classified before validating would accept an
    empty file as permission to re-run.
    """
    def sabotage(paused: _EnvGatePause) -> None:
        evidence = _meta(paused.run_dir)["phase_handoff"]["artifacts"][
            "gate_identities"
        ][1]["receipt_evidence"]
        (paused.run_dir / evidence).write_text("{}", encoding="utf-8")

    paused, session, before = _blocked_env_retry(tmp_path, sabotage)
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="schema_version",
        retry_offered=True,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_env_retry_blocked_by_a_receipt_with_no_subject(tmp_path: Path) -> None:
    """3h — without a subject block the receipt cannot say what it measured."""
    def sabotage(paused: _EnvGatePause) -> None:
        evidence = _meta(paused.run_dir)["phase_handoff"]["artifacts"][
            "gate_identities"
        ][0]["receipt_evidence"]
        path = paused.run_dir / evidence
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt.pop("subject")
        path.write_text(json.dumps(receipt), encoding="utf-8")

    paused, session, before = _blocked_env_retry(tmp_path, sabotage)
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="has no subject block",
        retry_offered=True,
    )


def _tamper_decision_id(paused: _EnvGatePause) -> None:
    """Rewrite the decision artifact's own id so strict validation rejects it.

    The file stays where the reader addresses it and still records
    ``retry_verification``; only its persisted id disagrees with the path —
    the shape a hand-edit or a torn write leaves behind.
    """
    path = (
        paused.run_dir / "phase_handoff_decisions"
        / f"{safe_handoff_id(paused.handoff_id)}.json"
    )
    decision = json.loads(path.read_text(encoding="utf-8"))
    decision["handoff_id"] = f"{paused.handoff_id}-tampered"
    path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_env_retry_blocked_by_a_corrupted_decision_id_on_both_surfaces(
    tmp_path: Path, surface: str,
) -> None:
    """3i — the *decision* is the damaged artifact, not the gate record.

    Every other 3x scenario damages what the retry points at; this one damages
    the operator's instruction itself. The strict reader is right to refuse it,
    but refusing it is the resume router's job — by then the pre-router guards
    have already retained this run's subject on the strength of the same
    artifact. So the refusal has to land as a decidable pause rather than a
    hard resume failure, or the run would be left holding a subject it can
    never move.
    """
    paused, session, before = _blocked_env_retry(
        tmp_path, _tamper_decision_id, resume=_RESUMES[surface],
    )
    _assert_reparked_without_executing(
        paused, session, before,
        reason_contains="failed strict validation",
        retry_offered=True,
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_a_corrupted_decision_id_still_stops_before_a_fresh_checkout(
    tmp_path: Path, surface: str,
) -> None:
    """3i + 4a — the damaged decision must not cost the subject either.

    The two defects compound: if a corrupted decision were read as an ordinary
    resume, the resolver would mint a clean checkout for the missing retained
    path *before* anyone noticed the corruption, and the tree the failing
    receipts observed would be gone for good.
    """
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)
    _tamper_decision_id(paused)

    shutil.rmtree(paused.retained)
    subprocess.run(["git", "worktree", "prune"], cwd=paused.project, check=True)
    before = _snapshot(paused)

    _expect_blocked_before_checkout(paused, surface)
    _assert_subject_loss_is_recoverable(paused, before)


# ── scenario 3-regression: the tolerance is scoped to the env retry ─────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_corrupt_ledger_without_an_env_retry_keeps_its_hard_refusal(
    tmp_path: Path,
) -> None:
    """The same corrupt ledger, decided ``continue_with_waiver``: still fatal.

    This is the counterweight to 3c. The ledger tolerance exists only because
    the env retry's *own owner* must be the component that judges that
    artifact; every other resume keeps reading it strictly, and a run that
    would have refused before must still refuse.
    """
    paused = _produce_env_gate_pause(tmp_path)
    _decide(
        paused, "continue_with_waiver",
        feedback="Accept the failed environment gates for this journey.",
    )
    (paused.run_dir / "scheduled_gate_ledger.json").write_text(
        "{not json at all", encoding="utf-8",
    )
    before = _snapshot(paused)

    with pytest.raises(ValueError, match="scheduled-gate ledger"):
        _resume_terminal(paused)

    # The refusal is a refusal, not a silently re-parked pause.
    assert _ledger_bytes(paused.run_dir) == before.ledger
    assert _meta(paused.run_dir).get("phase_handoff", {}).get(
        "id",
    ) != f"{paused.handoff_id}:retry_blocked"


# ── scenarios 4a–4c: the retained subject is gone, so nothing runs ──────────


def _expect_blocked_before_checkout(paused: _EnvGatePause, surface: str) -> None:
    """Both public surfaces stop the resume; only the error *shape* differs.

    TERMINAL prints the recoverable operator message and exits rc=2 the way
    the CLI does; SILENT re-raises the typed error for the embedder. Neither
    may fall through to the worktree resolver, which would mint a clean
    checkout and re-measure a tree the failing receipts never saw.
    """
    if surface == "terminal":
        with pytest.raises(SystemExit) as excinfo:
            _resume_terminal(paused)
        assert excinfo.value.code == 2
    else:
        with pytest.raises(WorktreeConfigError):
            _resume_silent(paused)


def _assert_subject_loss_is_recoverable(
    paused: _EnvGatePause, before: _PreResume,
) -> None:
    """The run survives the stop: same pause, same recorded subject, no gate."""
    meta = _meta(paused.run_dir)
    assert meta["status"] == "awaiting_phase_handoff"
    assert meta["status"] != "interrupted"
    assert meta.get("halt_reason") != "interrupted"
    # The *same* handoff, not a re-park: no decision was consumed, so the
    # operator can restore the worktree and resume the identical retry.
    assert meta["phase_handoff"]["id"] == paused.handoff_id
    assert meta["worktree"]["path"] == str(paused.retained)
    continuity = meta["worktree"]["resume_continuity"]
    assert continuity["blocked"] is True
    assert continuity["mode_label"].startswith("blocked:")
    assert _ledger_bytes(paused.run_dir) == before.ledger
    assert _decision_artifacts(paused.run_dir) == before.decisions
    assert _worktree_dirs(paused.worktrees_dir) == before.worktrees
    assert _worktree_registry(paused.project) == before.registry


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_env_retry_stops_when_the_retained_worktree_was_deleted(
    tmp_path: Path, surface: str,
) -> None:
    """4a — the checkout is gone and pruned from the registry."""
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)

    shutil.rmtree(paused.retained)
    subprocess.run(["git", "worktree", "prune"], cwd=paused.project, check=True)
    before = _snapshot(paused)

    _expect_blocked_before_checkout(paused, surface)
    _assert_subject_loss_is_recoverable(paused, before)


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_env_retry_stops_when_the_retained_worktree_was_unregistered(
    tmp_path: Path, surface: str,
) -> None:
    """4b — the directory is still there, but git no longer owns it.

    The harder half of 4a: a path check alone would call this subject
    available and re-measure a detached directory the source repo cannot
    relate to the run's base ref.
    """
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)

    shutil.rmtree(paused.project / ".git" / "worktrees")
    assert paused.retained.is_dir()
    before = _snapshot(paused)

    _expect_blocked_before_checkout(paused, surface)
    _assert_subject_loss_is_recoverable(paused, before)


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.parametrize("surface", sorted(_RESUMES))
def test_env_retry_stops_when_the_retained_worktree_was_reclaimed(
    tmp_path: Path, surface: str,
) -> None:
    """4c — workspace cleanup archived it: the recorded path is historical.

    The checkout may still be sitting there and still be registered; what the
    ``reclaimed`` marker says is that its contents are no longer the run's.
    So the block must land before the path is ever probed.
    """
    paused = _produce_env_gate_pause(tmp_path)
    _decide_retry_verification(paused)

    meta = _meta(paused.run_dir)
    meta["worktree"]["reclaimed"] = {"at": "2026-09-17T00:00:00Z"}
    _write_meta(paused.run_dir, meta)
    before = _snapshot(paused)

    _expect_blocked_before_checkout(paused, surface)
    _assert_subject_loss_is_recoverable(paused, before)
    assert _meta(paused.run_dir)["worktree"]["reclaimed"] == {
        "at": "2026-09-17T00:00:00Z",
    }
