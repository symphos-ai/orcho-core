"""A gate hook that ends with MORE THAN ONE required command red.

Regression cover for the production escape in run ``20260831_170837_de791f``:
``lint`` and ``typecheck`` both failed after ``implement``, but routing acted on
the first blocking disposition only — ``typecheck`` was never even executed at
that hook, never entered the repair loop, and never appeared in the operator's
``gate:lint:1`` handoff, so a ``continue`` was taken on a strict subset of the
blocking failures.

The subprocess + FSM boundaries are monkeypatched (``_run_gate_command`` /
``_dispatch_repair`` / ``_repair_step``) so routing is exercised with a
duck-typed run object — no real agent, worktree, or review pass.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.evidence.verification_receipt import subject_identity
from pipeline.plugins import PluginConfig
from pipeline.project import gate_repair
from pipeline.verification_contract import PlaceholderContext, VerificationContract
from pipeline.verification_failure import classify_receipt

COMMANDS = ("lint", "typecheck", "vitest")


def _contract(**verification) -> VerificationContract:
    """The lesson-editor shape: one gate set, three required commands."""
    base = {
        "commands": {
            "lint": {"run": "npm run lint", "cost": "fast"},
            "typecheck": {"run": "npx vue-tsc --noEmit", "cost": "fast"},
            "vitest": {"run": "npx vitest run", "cost": "moderate"},
        },
        "required": list(COMMANDS),
        "gate_sets": {"smoke": {"commands": list(COMMANDS)}},
        "selection": [{"always": ["smoke"]}],
        "schedule": [
            {
                "after_phase": "implement",
                "policy": "require",
                "action": "repair_loop",
                "commands": list(COMMANDS),
            },
        ],
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
    stdout: str = "out",
    stderr: str = "err",
) -> dict:
    return {
        "schema_version": 3,
        "exit_code": exit_code,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "assertions": assertions or [],
        "detail": detail,
        "subject": {"status": "available", "identity": {
            "version": 1, "object_format": "sha1", "tree_oid": "a" * 40,
            "observed_head_oid": "b" * 40, "baseline_oid": None,
        }},
        "dependencies": [],
    }


def _import_assertion_receipt() -> dict:
    """An exit-0 receipt whose provenance assertion failed (agent-unfixable)."""
    return _receipt(0, assertions=[{
        "name": "pipeline",
        "kind": "import_path_equals",
        "expected": "/work/pipeline/__init__.py",
        "actual": "/installed/pipeline/__init__.py",
        "passed": False,
    }])


def _patch_gates(monkeypatch, per_command: dict[str, list[dict]]) -> list[str]:
    """Serve a per-command receipt queue; return the execution order log."""
    order: list[str] = []
    queues = {command: list(results) for command, results in per_command.items()}

    def fake_gate(run, contract, entry):
        order.append(entry.command)
        queue = queues[entry.command]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(gate_repair, "_run_gate_command", fake_gate)
    monkeypatch.setattr(
        gate_repair,
        "_classify_gate_receipt",
        lambda receipt, _ctx: classify_receipt(
            receipt, current_subject=subject_identity(receipt.get("subject")),
        ),
    )
    return order


def _patch_repair(monkeypatch, *, halt: bool = False) -> list[str]:
    """Record the critique each dispatched repair round was handed."""
    monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: object())
    critiques: list[str] = []

    def fake_dispatch(run, repair_step, ctx, *, round_n, max_rounds):
        critiques.append(run.state.last_critique)
        if halt:
            run.state.stop("repair halted")

    monkeypatch.setattr(gate_repair, "_dispatch_repair", fake_dispatch)
    return critiques


def _failed_delivery_statuses(commands: dict[str, dict]) -> dict:
    """``_delivery_receipt_statuses`` shape for already-materialized failures."""
    return {
        command: (
            SimpleNamespace(
                status="failed",
                failure_kind="test_failure",
                exit_code=receipt["exit_code"],
                assertions_passed=0,
                assertions_total=0,
                failed_assertions=(),
                reason="",
            ),
            receipt,
        )
        for command, receipt in commands.items()
    }


# ── the hook executes the whole selected set before it routes ────────────────


def test_every_selected_gate_runs_even_after_the_first_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root cause: routing on the first blocking disposition meant the
    second failing required command was never executed at all, so it could not
    be repaired and left no failing receipt for this hook."""
    contract = _contract()
    run = _run(contract)
    order = _patch_gates(monkeypatch, {
        "lint": [_receipt(1), _receipt(0)],
        "typecheck": [_receipt(1)],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert order[:3] == ["lint", "typecheck", "vitest"]


def test_repair_critique_carries_every_failing_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0081 acceptance (a): the failed command output IS the critique, so a
    two-command failure set must hand the repair agent both outputs."""
    contract = _contract()
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_receipt(1, stdout="eslint: 3 problems")],
        "typecheck": [_receipt(1, stdout="vue-tsc: 18 errors")],
        "vitest": [_receipt(0)],
    })
    critiques = _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert critiques, "repair was never dispatched"
    first = critiques[0]
    assert "Command: lint" in first
    assert "Command: typecheck" in first
    assert "eslint: 3 problems" in first
    assert "vue-tsc: 18 errors" in first
    assert "2 required verification gates failed: lint, typecheck" in first
    # The test-output channel repair reads is aggregated the same way.
    assert "eslint: 3 problems" in run.state.last_test_output
    assert "vue-tsc: 18 errors" in run.state.last_test_output


def test_phase_does_not_pass_while_any_required_command_is_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance (b): under ``policy: require`` a repair that fixes one of two
    red required commands is not a passed phase — the run must pause, not walk
    into review/final_acceptance carrying a red required receipt."""
    contract = _contract(schedule=[{
        "after_phase": "implement", "policy": "require",
        "action": "repair_loop", "commands": list(COMMANDS),
    }])
    run = _run(contract, max_rounds=2)
    _patch_gates(monkeypatch, {
        # lint is repaired on the first recheck; typecheck stays red forever.
        "lint": [_receipt(1), _receipt(0)],
        "typecheck": [_receipt(1)],
        "vitest": [_receipt(0)],
    })
    critiques = _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.paused
    assert outcome.passed is False
    assert outcome.rounds == 2
    # Round 2 no longer chases the repaired command, only the still-red one.
    assert "Command: typecheck" in critiques[1]
    assert "Command: lint" not in critiques[1]
    signal = run.state.phase_handoff_request
    assert signal is not None
    assert [f["command"] for f in signal.artifacts["findings"]] == ["typecheck"]


def test_repair_loop_closes_only_when_every_failure_rechecks_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    run = _run(contract, max_rounds=3)
    _patch_gates(monkeypatch, {
        "lint": [_receipt(1), _receipt(0)],
        "typecheck": [_receipt(1), _receipt(0)],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.passed
    assert outcome.rounds == 1
    assert run.state.phase_handoff_request is None


# ── the operator decision surface names every blocking failure ───────────────


def test_gate_handoff_lists_every_failed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance (c): the payload the operator decides on must never be a
    strict subset of the blocking failures."""
    contract = _contract(schedule=[{
        "after_phase": "implement", "policy": "require",
        "action": "handoff", "commands": list(COMMANDS),
    }])
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_receipt(1, stdout="eslint: 3 problems")],
        "typecheck": [_receipt(1, stdout="vue-tsc: 18 errors")],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.paused
    artifacts = run.state.phase_handoff_request.artifacts
    assert [f["command"] for f in artifacts["findings"]] == ["lint", "typecheck"]
    assert artifacts["gate_commands"] == ["lint", "typecheck"]
    assert artifacts["gate_identities"] == [
        {"command": "lint", "hook": "after_phase", "phase": "implement"},
        {"command": "typecheck", "hook": "after_phase", "phase": "implement"},
    ]
    # The singular keys stay single-identity: waiver identity and handoff-route
    # classification are single-identity contracts.
    assert artifacts["gate_command"] == "lint"
    assert artifacts["gate_identity"] == artifacts["gate_identities"][0]
    assert "lint:" in artifacts["short_summary"]
    assert "typecheck:" in artifacts["short_summary"]


def test_before_delivery_handoff_lists_every_failed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production shape: the pre-final materializer had already produced a
    failed receipt for BOTH lint and typecheck, and the delivery hook reused
    them. The pause it raised named only lint."""
    contract = _contract(schedule=[{
        "before_delivery": True, "policy": "require",
        "action": "handoff", "commands": list(COMMANDS),
    }])
    run = _run(contract)
    _patch_gates(monkeypatch, {"vitest": [_receipt(0)]})
    _patch_repair(monkeypatch)
    monkeypatch.setattr(
        gate_repair,
        "_delivery_receipt_statuses",
        lambda _run, _contract: _failed_delivery_statuses({
            "lint": _receipt(1, stdout="eslint: 3 problems"),
            "typecheck": _receipt(1, stdout="vue-tsc: 18 errors"),
        }),
    )

    outcome = gate_repair.run_gate_hook(
        run, object(), object(), hook="before_delivery",
    )

    assert outcome.active and outcome.paused
    artifacts = run.state.phase_handoff_request.artifacts
    assert [f["command"] for f in artifacts["findings"]] == ["lint", "typecheck"]
    assert artifacts["gate_identities"] == [
        {"command": "lint", "hook": "before_delivery", "phase": ""},
        {"command": "typecheck", "hook": "before_delivery", "phase": ""},
    ]


def test_mixed_repairable_and_agent_unfixable_set_escalates_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One agent-unfixable member escalates the SET. Burning repair rounds on
    the fixable half and then showing the operator only that half is exactly
    the reported failure mode."""
    contract = _contract()
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_receipt(1)],
        "typecheck": [_import_assertion_receipt()],
        "vitest": [_receipt(0)],
    })
    critiques = _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.paused and outcome.rounds == 0
    assert critiques == []  # no repair round burned
    findings = run.state.phase_handoff_request.artifacts["findings"]
    assert [f["command"] for f in findings] == ["lint", "typecheck"]
    assert [f["failure_kind"] for f in findings] == [
        "test_failure", "provenance_failure",
    ]
    # A still-agent-fixable member keeps a repair retry on the table.
    assert "retry_feedback" in run.state.phase_handoff_request.available_actions


def test_all_agent_unfixable_set_offers_waiver_or_halt_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_import_assertion_receipt()],
        "typecheck": [_import_assertion_receipt()],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.paused
    assert run.state.phase_handoff_request.available_actions == (
        "continue_with_waiver", "halt",
    )


def test_abort_still_short_circuits_the_remaining_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``abort`` ends the run, so there is no aggregate decision surface left
    to complete and no reason to spend the remaining gates' wall-clock."""
    contract = _contract(schedule=[{
        "after_phase": "implement", "policy": "require",
        "action": "abort", "commands": list(COMMANDS),
    }])
    run = _run(contract)
    order = _patch_gates(monkeypatch, {
        "lint": [_receipt(1)],
        "typecheck": [_receipt(1)],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.active and outcome.halted
    assert order == ["lint"]
    assert run.state.phase_handoff_request is None
    assert run.session.get("status") == "halted"


def test_non_blocking_failures_never_join_the_blocking_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``continue_warn`` failures warn and are consumed: they must not show up
    in a handoff as though they were blocking."""
    contract = _contract(schedule=[
        {
            "after_phase": "implement", "policy": "require",
            "action": "handoff", "commands": ["lint"],
        },
        {
            "after_phase": "implement", "policy": "require",
            "action": "continue_warn", "commands": ["typecheck"],
        },
    ])
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_receipt(1)],
        "typecheck": [_receipt(1)],
    })
    _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.paused
    findings = run.state.phase_handoff_request.artifacts["findings"]
    assert [f["command"] for f in findings] == ["lint"]
