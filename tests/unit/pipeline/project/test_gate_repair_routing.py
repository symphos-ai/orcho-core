"""Unit tests for pipeline/project/gate_repair.py (Stage 4 critical flow).

The subprocess + FSM boundaries are monkeypatched (``_run_gate_command`` /
``_dispatch_repair`` / ``_repair_step``) so the routing logic is exercised with
a duck-typed run object — no real agent, worktree, or review pass.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.plugins import PluginConfig
from pipeline.project import gate_repair
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)


def _contract(**verification) -> VerificationContract:
    base = {
        "commands": {"test": {"run": "pytest", "cheap": True}},
        "required": ["test"],
        "gate_sets": {"core": {"commands": ["test"]}},
        "selection": [{"always": ["core"]}],
        "schedule": [{"after_phase": "implement", "commands": ["test"]}],
    }
    base.update(verification)
    contract = VerificationContract.from_plugin(
        PluginConfig(work_mode="governed", verification=base),
    )
    assert contract is not None
    return contract


class _State:
    def __init__(self, contract) -> None:
        self.extras = {
            "verification_contract": contract,
            "verification_placeholders": PlaceholderContext(checkout=""),
        }
        self.last_critique = ""
        self.last_test_output = ""
        self.halt = False
        self.halt_reason = ""
        self.phase_handoff_request = None

    def stop(self, reason: str) -> None:
        self.halt = True
        self.halt_reason = reason


def _run(contract, *, max_rounds: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        state=_State(contract),
        session={},
        max_rounds=max_rounds,
        _on_phase_start=None,
        _on_phase_end=None,
    )


def _receipt(
    exit_code: int | None,
    *,
    assertions: list[dict] | None = None,
    detail: str = "",
) -> dict:
    return {
        "exit_code": exit_code,
        "stdout_tail": "out",
        "stderr_tail": "err",
        "assertions": assertions or [],
        "detail": detail,
    }


def test_passed_is_authoritative_not_just_exit_code() -> None:
    """``_passed`` rejects an exit-0 receipt with a failed assertion or a
    non-empty detail, matching the readiness/delivery rollup."""
    assert gate_repair._passed(_receipt(0)) is True
    assert gate_repair._passed(_receipt(1)) is False
    assert (
        gate_repair._passed(
            _receipt(0, assertions=[{"name": "x", "passed": False}]),
        )
        is False
    )
    assert gate_repair._passed(_receipt(0, detail="baseline regression")) is False


def test_exit0_failed_assertion_gate_handoffs_without_repair(monkeypatch) -> None:
    """A scheduled gate whose receipt exits 0 but fails an assertion is routed as
    failed and pauses for an operator waiver, never a false-green close or
    agent repair."""
    contract = _contract()
    run = _run(contract)
    calls = _patch_gate_results(
        monkeypatch,
        [
            _receipt(0, assertions=[{"name": "no-warnings", "passed": False}]),
            _receipt(0),
        ],
    )
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.paused
    assert outcome.rounds == 0
    assert calls["repair"] == 0
    signal = run.state.phase_handoff_request
    assert signal is not None
    assert signal.available_actions == ("continue_with_waiver", "halt")
    finding = signal.artifacts["findings"][0]
    assert finding["failure_kind"] == "env_failure"
    assert "exit_code=0" in finding["body"]
    assert "assertions=0/1 passed" in finding["body"]
    assert signal.artifacts["short_summary"] == finding["body"]
    assert signal.last_output
    # The durable routing trail recorded the first run as executed_fail.
    events = run.state.extras[gate_repair.VERIFICATION_GATE_EVENTS_KEY]
    decisions = [e["decision"] for e in events if e.get("command") == "test"]
    assert "executed_fail" in decisions


def _patch_gate_results(monkeypatch, results: list[dict]) -> dict:
    """Feed a queue of receipts; track how many gate runs happened."""
    calls = {"gate": 0, "repair": 0}
    queue = list(results)

    def fake_gate(run, contract, entry):
        calls["gate"] += 1
        return queue.pop(0) if queue else results[-1]

    monkeypatch.setattr(gate_repair, "_run_gate_command", fake_gate)
    return calls


def _patch_repair(monkeypatch, calls: dict, *, halt: bool = False) -> None:
    monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: object())

    def fake_dispatch(run, repair_step, ctx, *, round_n, max_rounds):
        calls["repair"] += 1
        if halt:
            run.state.stop("repair halted")

    monkeypatch.setattr(gate_repair, "_dispatch_repair", fake_dispatch)


def test_no_contract_branch_inactive() -> None:
    run = SimpleNamespace(state=SimpleNamespace(extras={}))
    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())
    assert outcome.active is False


def test_passing_gate_closes_without_repair(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract)
    calls = _patch_gate_results(monkeypatch, [_receipt(0)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.passed
    assert calls["repair"] == 0
    assert run.state.phase_handoff_request is None


def test_failed_gate_enters_repair_without_review(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract)
    # fail once, pass on the re-check after one repair round.
    calls = _patch_gate_results(monkeypatch, [_receipt(1), _receipt(0)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.passed
    assert outcome.rounds == 1
    assert calls["repair"] == 1  # repair_changes dispatched
    assert calls["gate"] == 2  # initial fail + passing re-check
    # the failed command output became the critique (no reviewer pass).
    assert "Required verification gate failed" in run.state.last_critique
    assert run.state.phase_handoff_request is None


def test_test_failure_critique_keeps_receipt_tails_for_repair(monkeypatch) -> None:
    """Repair receives full command evidence as well as the compact summary."""
    contract = _contract()
    run = _run(contract)
    receipt = _receipt(1)
    receipt["stdout_tail"] = "FAILED tests/test_gate.py::test_expected_value"
    receipt["stderr_tail"] = "Traceback: expected 1, got 0"
    calls = _patch_gate_results(monkeypatch, [receipt, _receipt(0)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.passed
    assert "class=test_failure" in run.state.last_test_output
    assert "FAILED tests/test_gate.py::test_expected_value" in run.state.last_test_output
    assert "Traceback: expected 1, got 0" in run.state.last_test_output
    assert "stdout:\nFAILED tests/test_gate.py::test_expected_value" in run.state.last_critique
    assert "stderr:\nTraceback: expected 1, got 0" in run.state.last_critique


def test_exit0_import_assertion_handoff_has_provenance_evidence(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract)
    calls = _patch_gate_results(
        monkeypatch,
        [
            _receipt(
                0,
                assertions=[
                    {
                        "name": "pipeline",
                        "kind": "import_path_equals",
                        "expected": "/work/pipeline/__init__.py",
                        "actual": "/installed/pipeline/__init__.py",
                        "passed": False,
                    }
                ],
            )
        ],
    )
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.paused and calls["repair"] == 0
    signal = run.state.phase_handoff_request
    assert signal.artifacts["findings"] == [
        {
            "id": "verification_gate_provenance_failure",
            "severity": "P3",
            "title": "Verification gate provenance_failure",
            "body": signal.artifacts["short_summary"],
            "required_fix": "Fix the verification environment outside the agent or choose an explicit waiver.",
            "failure_kind": "provenance_failure",
        }
    ]
    assert "expected='/work/pipeline/__init__.py'" in signal.last_output
    assert "actual='/installed/pipeline/__init__.py'" in signal.last_output


def test_recheck_is_exit_condition_after_multiple_rounds(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract, max_rounds=3)
    calls = _patch_gate_results(
        monkeypatch,
        [_receipt(1), _receipt(1), _receipt(0)],
    )
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.passed and outcome.rounds == 2
    assert run.state.phase_handoff_request is None


def test_recheck_hygiene_failure_pauses_without_another_repair(monkeypatch) -> None:
    """A repair recheck that becomes hygiene evidence exits to waiver handoff."""
    contract = _contract()
    run = _run(contract, max_rounds=3)
    calls = _patch_gate_results(
        monkeypatch,
        [
            _receipt(1),
            _receipt(
                0,
                assertions=[
                    {
                        "name": "pipeline",
                        "kind": "import_path_equals",
                        "expected": "/work/pipeline/__init__.py",
                        "actual": "/installed/pipeline/__init__.py",
                        "passed": False,
                    }
                ],
            ),
        ],
    )
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.paused and outcome.rounds == 1
    assert calls["repair"] == 1
    signal = run.state.phase_handoff_request
    assert signal is not None
    assert signal.available_actions == ("continue_with_waiver", "halt")
    assert signal.artifacts["findings"][0]["failure_kind"] == "provenance_failure"


def test_budget_exhaustion_escalates_to_handoff(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract, max_rounds=2)
    calls = _patch_gate_results(monkeypatch, [_receipt(1)])  # always fails
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.paused
    assert outcome.rounds == 2
    assert calls["repair"] == 2
    assert run.state.phase_handoff_request is not None
    assert run.state.phase_handoff_request.phase == "implement"
    assert run.state.halt is True
    assert "retry_feedback" in run.state.phase_handoff_request.available_actions


def test_action_handoff_escalates_immediately(monkeypatch) -> None:
    contract = _contract(
        schedule=[{"after_phase": "implement", "policy": "require", "action": "handoff", "commands": ["test"]}],
    )
    run = _run(contract)
    calls = _patch_gate_results(monkeypatch, [_receipt(1)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.paused
    assert calls["repair"] == 0  # no repair attempted
    assert run.state.phase_handoff_request is not None


def test_action_abort_halts(monkeypatch) -> None:
    contract = _contract(
        schedule=[{"after_phase": "implement", "policy": "require", "action": "abort", "commands": ["test"]}],
    )
    run = _run(contract)
    calls = _patch_gate_results(monkeypatch, [_receipt(1)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.halted
    assert calls["repair"] == 0
    assert run.state.phase_handoff_request is None
    assert run.state.halt is True
    assert run.session.get("status") == "halted"


def test_repair_halt_during_dispatch_returns_halted(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract)
    calls = _patch_gate_results(monkeypatch, [_receipt(1), _receipt(1)])
    _patch_repair(monkeypatch, calls, halt=True)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.halted
    assert calls["repair"] == 1


def test_no_repair_step_falls_back_to_handoff(monkeypatch) -> None:
    contract = _contract()
    run = _run(contract)
    _patch_gate_results(monkeypatch, [_receipt(1)])
    monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: None)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.paused
    assert run.state.phase_handoff_request is not None


# ── Receipt persistence (ADR 0090) ───────────────────────────────────────────


def _fake_command_receipt(exit_code: int) -> dict:
    """Minimal Stage 3 run_command payload the writer can persist."""
    return {
        "kind": "verification_command",
        "command": "test",
        "env": "",
        "cwd": "/tmp/wt",
        "placeholders": {"checkout": "/tmp/wt", "project": "/tmp/p"},
        "argv": ["pytest"],
        "env_overrides": {},
        "assertions": [],
        "exit_code": exit_code,
        "duration_s": 0.1,
        "stdout_tail": "",
        "stderr_tail": "",
        "log_path": None,
        "parity": "absolute",
        "detail": "",
        "git": {
            "checkout_head": None,
            "baseline_head": None,
            "changed_files_fingerprint": None,
        },
        "dependencies": [],
    }


def test_run_gate_command_persists_receipt(monkeypatch, tmp_path) -> None:
    """The executed gate receipt must land on disk so readiness / the
    delivery gate / evidence see the same proof routing acted on."""
    import pipeline.verification_command as vc
    from pipeline.evidence.verification_receipt import load_command_receipts

    contract = _contract()
    run = _run(contract)
    run.state.output_dir = tmp_path
    monkeypatch.setattr(
        vc,
        "run_command",
        lambda *a, **k: _fake_command_receipt(0),
    )

    entry = SimpleNamespace(command="test")
    receipt = gate_repair._run_gate_command(run, contract, entry)

    assert receipt["exit_code"] == 0
    persisted = load_command_receipts(tmp_path)
    assert [r["command"] for r in persisted] == ["test"]
    assert persisted[0]["exit_code"] == 0


def test_run_gate_command_overwrites_receipt_on_rerun(
    monkeypatch,
    tmp_path,
) -> None:
    import pipeline.verification_command as vc
    from pipeline.evidence.verification_receipt import load_command_receipts

    contract = _contract()
    run = _run(contract)
    run.state.output_dir = tmp_path
    queue = [_fake_command_receipt(1), _fake_command_receipt(0)]
    monkeypatch.setattr(vc, "run_command", lambda *a, **k: queue.pop(0))

    entry = SimpleNamespace(command="test")
    gate_repair._run_gate_command(run, contract, entry)
    gate_repair._run_gate_command(run, contract, entry)

    persisted = load_command_receipts(tmp_path)
    assert len(persisted) == 1
    assert persisted[0]["exit_code"] == 0


def test_run_gate_command_tolerates_missing_output_dir(
    monkeypatch,
) -> None:
    import pipeline.verification_command as vc

    contract = _contract()
    run = _run(contract)
    run.state.output_dir = None
    monkeypatch.setattr(
        vc,
        "run_command",
        lambda *a, **k: _fake_command_receipt(0),
    )

    entry = SimpleNamespace(command="test")
    receipt = gate_repair._run_gate_command(run, contract, entry)

    assert receipt["exit_code"] == 0
