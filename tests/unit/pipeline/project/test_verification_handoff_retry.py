from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from pipeline.control.handoff_routing import GateIdentity
from pipeline.plugins import PluginConfig
from pipeline.project.verification_handoff_retry import (
    VerificationHandoffRetryBlocked,
    VerificationHandoffRetryContext,
    apply_verification_handoff_resume,
    apply_verification_handoff_retry,
)
from pipeline.project.verification_ledger_runtime import (
    initialize,
    record_execution,
    select_epoch,
)
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger_store import load_ledger
from pipeline.verification_selection import SelectionContext


def _run() -> SimpleNamespace:
    active = {
        "id": "gate:pytest-unit:1", "round": 1,
        "phase": "final_acceptance", "trigger": "verification_gate_failed",
    }
    return SimpleNamespace(
        session={"phase_handoff": active, "status": "awaiting_phase_handoff"},
        state=SimpleNamespace(extras={}, human_feedback="", halt=False, phase_handoff_request=None),
        output_dir=None,
    )


def test_phase_handoff_dispatches_verification_retry_with_a_fresh_subject(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final-acceptance seam dispatches by trigger, not phase.

    This covers the incident path through ``apply_phase_handoff_resume`` rather
    than only the pure classifier: the exact persisted decision reaches the
    verification owner, while the retained checkout receives one repair before
    the selected gate observes a different typed subject.
    """
    from pipeline.project import handoff, verification_handoff_retry
    from pipeline.verification_subject import (
        VerificationSubjectAvailable,
        capture_verification_subject,
    )

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@orcho.invalid"],
        ["git", "config", "user.name", "Orcho Test"],
    ):
        subprocess.run(argv, cwd=checkout, check=True)
    (checkout / "subject.txt").write_text("before repair\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=checkout, check=True)

    run = _run()
    run.output_dir = tmp_path / "run"
    run.output_dir.mkdir()
    active = run.session["phase_handoff"]
    active["artifacts"] = {"gate_identity": {
        "command": "pytest-unit", "hook": "after_phase", "phase": "implement",
    }}
    decision = SimpleNamespace(
        action="retry_feedback", feedback="Исправьте проверку", note="operator note",
        decided_at="2026-07-21T20:41:39Z",
    )
    monkeypatch.setattr(handoff, "load_handoff_decision_validated", lambda *_args: decision)
    monkeypatch.setattr(
        handoff, "_apply_scope_expansion_handoff_resume",
        lambda *_args, **_kwargs: pytest.fail("verification retry must not enter scope expansion"),
    )
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(handoff, "_persist_handoff_running_state", lambda _run: None)

    dispatched: dict[str, object] = {}
    original_dispatch = verification_handoff_retry.apply_verification_handoff_resume

    def _verification_dispatch(**kwargs):
        dispatched.update(kwargs)
        return original_dispatch(**kwargs)

    monkeypatch.setattr(
        verification_handoff_retry, "apply_verification_handoff_resume", _verification_dispatch,
    )
    subjects: dict[str, object] = {}

    def _repair(*_args, **kwargs) -> None:
        retry_context = kwargs["retry_context"]
        assert retry_context.fresh_round == 2
        assert retry_context.loop_max_rounds == 1
        captured = capture_verification_subject(checkout)
        assert isinstance(captured, VerificationSubjectAvailable)
        subjects["repair"] = captured.identity
        (checkout / "subject.txt").write_text("after repair\n", encoding="utf-8")

    def _rerun(_run, **kwargs) -> bool:
        captured = capture_verification_subject(checkout)
        assert isinstance(captured, VerificationSubjectAvailable)
        subjects["rerun"] = captured.identity
        assert kwargs["retry_context"].identity == GateIdentity(
            "pytest-unit", "after_phase", "implement",
        )
        assert kwargs["retry_context"].fresh_round == 2
        return True

    before_repair = capture_verification_subject(checkout)
    assert isinstance(before_repair, VerificationSubjectAvailable)
    monkeypatch.setattr(verification_handoff_retry, "_dispatch_one_repair", _repair)
    monkeypatch.setattr("pipeline.project.gate_repair.rerun_verification_handoff_gate", _rerun)

    outcome = handoff.apply_phase_handoff_resume(run, profile=object(), ctx=object())

    assert outcome.paused is False
    assert dispatched["handoff_id"] == "gate:pytest-unit:1"
    assert dispatched["action"] == "retry_feedback"
    assert dispatched["identity"] == GateIdentity("pytest-unit", "after_phase", "implement")
    assert dispatched["feedback"] == "Исправьте проверку"
    assert dispatched["note"] == "operator note"
    assert dispatched["decided_at"] == "2026-07-21T20:41:39Z"
    assert subjects["repair"] == before_repair.identity
    assert subjects["rerun"] != subjects["repair"]


def test_retry_repairs_once_then_reruns_one_fresh_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    calls: list[object] = []
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry.guard_review_retry_subject",
        lambda _run: None,
        raising=False,
    )
    # The guard is imported locally, so supply its source seam too.
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **kwargs: calls.append({"repair": kwargs}),
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **kwargs: calls.append(kwargs) or True,
    )

    outcome = apply_verification_handoff_retry(
        run=run, profile=object(), ctx=object(), active={"round": 1},
        handoff_id="gate:pytest-unit:1", feedback="Поправьте тест", note=None,
        decided_at="2026-01-01T00:00:00Z",
        identity=GateIdentity("pytest-unit", "after_phase", "implement"),
    )

    assert outcome.paused is False
    assert calls == [{"repair": {
        "retry_context": VerificationHandoffRetryContext(
            identity=GateIdentity("pytest-unit", "after_phase", "implement"),
            prior_round=1, fresh_round=2, loop_max_rounds=1,
            human_retry_ordinal=1,
        ),
    }}, {
        "retry_context": VerificationHandoffRetryContext(
            identity=GateIdentity("pytest-unit", "after_phase", "implement"),
            prior_round=1, fresh_round=2, loop_max_rounds=1,
            human_retry_ordinal=1,
        ),
    }]
    assert run.state.human_feedback == "Поправьте тест"
    assert run.state.extras["phase_handoff_override"]["feedback"] == "Поправьте тест"


def test_repeated_consumption_does_not_run_second_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    run.session.pop("phase_handoff")
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: pytest.fail("must not repair"),
    )
    with pytest.raises(VerificationHandoffRetryBlocked, match="no longer matches"):
        apply_verification_handoff_retry(
            run=run, profile=object(), ctx=object(), active={"round": 1},
            handoff_id="gate:pytest-unit:1", feedback="same", note=None,
            decided_at="unchanged", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
        )


def test_second_failure_keeps_new_recovery_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: None,
    )

    def _fail(next_run, **_kwargs):
        next_run.state.phase_handoff_request = SimpleNamespace(handoff_id="gate:pytest-unit:2")
        return False

    monkeypatch.setattr("pipeline.project.gate_repair.rerun_verification_handoff_gate", _fail)
    outcome = apply_verification_handoff_retry(
        run=run, profile=object(), ctx=object(), active={"round": 1},
        handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
        decided_at="now", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
    )
    assert outcome.paused is True
    assert run.state.phase_handoff_request.handoff_id == "gate:pytest-unit:2"


def test_retry_snapshots_fsm_metrics_before_exact_gate_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: calls.append("repair"),
    )
    monkeypatch.setattr(
        "pipeline.project.handoff._persist_handoff_retry_metrics",
        lambda _run: calls.append("metrics"),
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: calls.append("rerun") or True,
    )

    apply_verification_handoff_retry(
        run=run, profile=object(), ctx=object(), active={"round": 2, "loop_max_rounds": 2},
        handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
        decided_at="now", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
    )

    assert calls == ["repair", "metrics", "rerun"]


def test_control_preflight_failure_preserves_active_recovery_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: None)

    with pytest.raises(VerificationHandoffRetryBlocked, match="no repair_changes"):
        apply_verification_handoff_retry(
            run=run, profile=object(), ctx=object(), active={"round": 1},
            handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
            decided_at="now", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
        )
    assert run.session["phase_handoff"]["id"] == "gate:pytest-unit:1"


@pytest.mark.parametrize("action", ["continue", "continue_with_waiver"])
def test_verification_continue_actions_close_gate_pause_without_plan_loop(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    run = _run()
    active = run.session["phase_handoff"]
    monkeypatch.setattr(
        "pipeline.project.handoff._persist_handoff_running_state", lambda _run: None,
    )

    outcome = apply_verification_handoff_resume(
        run=run, profile=object(), ctx=object(), active=active,
        handoff_id="gate:pytest-unit:1", action=action,
        feedback="Разрешено оператором" if action.endswith("waiver") else "",
        note=None, decided_at="now",
        identity=GateIdentity("pytest-unit", "after_phase", "implement"),
    )

    assert outcome.completed_phases == frozenset({"final_acceptance"})
    assert "phase_handoff" not in run.session
    assert "phase_handoff_waiver" in run.session if action.endswith("waiver") else "phase_handoff_waiver" not in run.session


def test_provider_crash_propagates_and_does_not_become_control_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.io.retry import AgentProcessKilledError

    run = _run()
    monkeypatch.setattr("pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None)
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AgentProcessKilledError("killed")),
    )

    with pytest.raises(AgentProcessKilledError, match="killed"):
        apply_verification_handoff_retry(
            run=run, profile=object(), ctx=object(), active=run.session["phase_handoff"],
            handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
            decided_at="now", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
        )


def test_control_failure_restores_subject_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    run = _run()
    run.output_dir = tmp_path
    monkeypatch.setattr("pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None)
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("control failed")),
    )

    with pytest.raises(VerificationHandoffRetryBlocked, match="control failed"):
        apply_verification_handoff_retry(
            run=run, profile=object(), ctx=object(), active=run.session["phase_handoff"],
            handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
            decided_at="now", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
        )

    import json
    persisted = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "awaiting_phase_handoff"
    assert persisted["phase_handoff"]["id"] == "gate:pytest-unit:1"


def test_dispatch_exposes_explicit_human_retry_round_to_lifecycle() -> None:
    from pipeline.lifecycle import default_lifecycle_context
    from pipeline.plugins import PluginConfig
    from pipeline.project import verification_handoff_retry
    from pipeline.runtime import PhaseRegistry, PhaseStep, PipelineState
    from pipeline.session_adapters import RoundAdapter, SessionAdapterRegistry

    registry = PhaseRegistry()

    def _repair(state):
        state.phase_log["rounds_pending"] = {"critique": "retry feedback"}
        state.phase_log["repair_changes"] = {"output": "fixed"}
        return state

    registry.register("repair_changes", _repair)
    adapters = SessionAdapterRegistry()
    adapters.register("repair_changes", RoundAdapter())
    session: dict[str, object] = {}
    ctx = default_lifecycle_context(
        phase_registry=registry,
        session_adapter_registry=adapters,
        run_config={"session": session},
    )
    state = PipelineState(task="fix gate", project_dir="/project", plugin=PluginConfig())
    run = SimpleNamespace(state=state)

    verification_handoff_retry._dispatch_one_repair(
        run,
        PhaseStep(phase="repair_changes"),
        ctx,
        retry_context=VerificationHandoffRetryContext(
            identity=GateIdentity("pytest-unit", "after_phase", "implement"),
            prior_round=1, fresh_round=2, loop_max_rounds=1,
            human_retry_ordinal=1,
        ),
    )

    assert state.extras == {"repair_round": 2, "repair_round_max": 1}
    assert session["phases"] == {
        "rounds": [{"round": 2, "critique": "retry feedback"}],
    }


def test_adapter_contract_failure_restores_recovery_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr("pipeline.project.gate_repair._repair_step", lambda _profile: object())
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("RoundAdapter requires explicit round_n"),
        ),
    )

    with pytest.raises(
        VerificationHandoffRetryBlocked,
        match="RoundAdapter requires explicit round_n",
    ):
        apply_verification_handoff_retry(
            run=run, profile=object(), ctx=object(), active=run.session["phase_handoff"],
            handoff_id="gate:pytest-unit:1", feedback="retry", note=None,
            decided_at="now", identity=GateIdentity("pytest-unit", "after_phase", "implement"),
        )

    assert run.session["status"] == "awaiting_phase_handoff"
    assert run.session["phase_handoff"]["id"] == "gate:pytest-unit:1"


def test_rerun_gate_executes_selected_identity_and_publishes_fresh_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real rerun owner rather than replacing it at the caller."""
    from pipeline.project import gate_repair

    entry = SimpleNamespace(
        command="pytest-unit", hook="after_phase", phase="implement",
        primary_gate_set="required", policy="require",
    )
    run = SimpleNamespace(
        state=SimpleNamespace(
            extras={}, phase_handoff_request=None, last_critique="",
            stop=lambda _reason: None,
        ),
    )
    monkeypatch.setattr(gate_repair, "_contract", lambda _run: object())
    monkeypatch.setattr(gate_repair, "_plan", lambda *_args, **_kwargs: SimpleNamespace(entries=[entry]))
    monkeypatch.setattr(gate_repair, "_run_gate_command", lambda *_args: {"exit_code": 1})
    monkeypatch.setattr(gate_repair, "_placeholders", lambda _run: object())
    monkeypatch.setattr(
        gate_repair, "_classify_gate_receipt",
        lambda *_args: SimpleNamespace(
            status="absent", failure_kind="test_failure", exit_code=1,
            assertions_passed=0, assertions_total=0, failed_assertions=(),
        ),
    )
    monkeypatch.setattr(gate_repair, "_record_executed_gate_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gate_repair, "_synthesize_critique", lambda *_args: None)

    passed = gate_repair.rerun_verification_handoff_gate(
        run, retry_context=VerificationHandoffRetryContext(
            identity=GateIdentity("pytest-unit", "after_phase", "implement"),
            prior_round=2, fresh_round=3, loop_max_rounds=2,
            human_retry_ordinal=1,
        ),
    )

    assert passed is False
    assert run.state.phase_handoff_request.handoff_id == "gate:pytest-unit:3"
    assert run.state.phase_handoff_request.round == 3
    assert run.state.phase_handoff_request.loop_max_rounds == 2
    assert run.state.phase_handoff_request.artifacts["gate_identity"] == {
        "command": "pytest-unit", "hook": "after_phase", "phase": "implement",
    }


def test_rerun_gate_records_fresh_rerun_execution_in_durable_ledger(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production rerun seam appends, rather than replaces, its execution."""
    from pipeline.project import gate_repair

    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {"pytest-unit": {"run": "pytest tests/unit"}},
        "gate_sets": {"required": {"commands": ["pytest-unit"]}},
        "selection": [{"always": ["required"]}],
        "schedule": [{
            "after_phase": "implement", "gate_sets": ["required"], "policy": "require",
        }],
    }))
    assert contract is not None
    run = SimpleNamespace(
        checkpoint_resume=False,
        state=SimpleNamespace(
            output_dir=tmp_path,
            extras={"verification_contract": contract},
            phase_handoff_request=None,
            last_critique="",
            stop=lambda _reason: None,
        ),
    )
    initialize(run.state)
    entry = select_epoch(
        run, contract, epoch="after_phase:implement", context=SelectionContext(),
    ).entries[0]
    record_execution(run, entry, passed=False, receipt_evidence="receipts/original.json")

    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_args, **_kwargs: {
            "command": "pytest-unit",
            "env": "",
            "cwd": "/tmp/wt",
            "placeholders": {"checkout": "/tmp/wt", "project": "/tmp/wt"},
            "argv": ["pytest", "tests/unit"],
            "env_overrides": {},
            "assertions": [],
            "exit_code": 1,
            "duration_s": 0.1,
            "stdout_tail": "",
            "stderr_tail": "failed",
            "log_path": None,
            "parity": "absolute",
            "detail": "",
            "git": {"checkout_head": None, "baseline_head": None},
            "dependencies": [],
        },
    )
    monkeypatch.setattr(
        gate_repair,
        "_classify_gate_receipt",
        lambda *_args: SimpleNamespace(
            status="absent", failure_kind="test_failure", exit_code=1,
            assertions_passed=0, assertions_total=0, failed_assertions=(),
        ),
    )
    monkeypatch.setattr(gate_repair, "_synthesize_critique", lambda *_args: None)

    assert gate_repair.rerun_verification_handoff_gate(
        run,
        retry_context=VerificationHandoffRetryContext(
            identity=GateIdentity("pytest-unit", "after_phase", "implement"),
            prior_round=2,
            fresh_round=3,
            loop_max_rounds=2,
            human_retry_ordinal=1,
        ),
    ) is False

    executions = [event for event in load_ledger(tmp_path).trail if event.kind == "execution"]
    assert [(event.identity, event.receipt_evidence, event.rerun) for event in executions] == [
        (("pytest-unit", "after_phase", "implement"), "receipts/original.json", False),
        (("pytest-unit", "after_phase", "implement"),
         "verification_command_receipts/executions/"
         "pytest-unit--after_phase--implement--0001.json", True),
    ]
    rerun_path = tmp_path / executions[-1].receipt_evidence
    assert rerun_path.is_file()


def test_rerun_gate_passes_only_for_the_selected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project import gate_repair

    entry = SimpleNamespace(
        command="pytest-unit", hook="after_phase", phase="implement", policy="require",
    )
    run = SimpleNamespace(state=SimpleNamespace(extras={}))
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(gate_repair, "_contract", lambda _run: object())
    monkeypatch.setattr(gate_repair, "_plan", lambda *_args, **_kwargs: SimpleNamespace(entries=[entry]))
    monkeypatch.setattr(
        gate_repair, "_run_gate_command",
        lambda _run, _contract, selected: calls.append(
            (selected.command, selected.hook, selected.phase),
        ) or {"exit_code": 0},
    )
    monkeypatch.setattr(gate_repair, "_placeholders", lambda _run: object())
    monkeypatch.setattr(
        gate_repair, "_classify_gate_receipt", lambda *_args: SimpleNamespace(status="present"),
    )
    monkeypatch.setattr(gate_repair, "_record_executed_gate_event", lambda *_args, **_kwargs: None)

    assert gate_repair.rerun_verification_handoff_gate(
        run, retry_context=VerificationHandoffRetryContext(
            identity=GateIdentity("pytest-unit", "after_phase", "implement"),
            prior_round=1, fresh_round=2, loop_max_rounds=1,
            human_retry_ordinal=1,
        ),
    ) is True
    assert calls == [("pytest-unit", "after_phase", "implement")]


def test_retry_context_preserves_exhausted_automatic_budget() -> None:
    from pipeline.control.handoff_labels import render_round_label

    context = VerificationHandoffRetryContext.from_active(
        {"round": 2, "loop_max_rounds": 2},
        GateIdentity("pytest-unit", "after_phase", "implement"),
    )

    assert context.fresh_round == 3
    assert context.loop_max_rounds == 2
    assert context.human_retry_ordinal == 1
    assert render_round_label(
        phase="implement", round=context.fresh_round,
        loop_max_rounds=context.loop_max_rounds, human_directed=True,
    ) == "implement human retry 1 after REJECTED verdict"


def test_legacy_gate_identity_is_loaded_from_the_parent_ledger(tmp_path) -> None:
    from pipeline.project.handoff import _scheduled_gate_identities
    from pipeline.verification_ledger import GateLedgerRow
    from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger

    write_ledger(tmp_path, ScheduledGateLedger((GateLedgerRow(
        gate="pytest-unit", hook="after_phase", phase="implement",
        timing="after_implement", run_mode="auto", gate_sets=(), condition="always",
    ),)))

    assert _scheduled_gate_identities(SimpleNamespace(output_dir=tmp_path)) == (
        GateIdentity("pytest-unit", "after_phase", "implement"),
    )
