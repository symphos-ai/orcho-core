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
from pipeline.project import (
    gate_failure_set,
    gate_handoff_actions,
    gate_repair,
)
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
        # Read by the loop-boundary verdict routing records on a mid-loop pause.
        self.phase_log: dict = {}
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
    evidence: str | None = None,
) -> dict:
    receipt = {
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
    if evidence is not None:
        # What ``_persist_gate_receipt`` stamps once the immutable evidence
        # file lands; the subprocess seam is patched out here, so tests that
        # need a proven receipt supply it themselves.
        receipt[gate_failure_set.RECEIPT_EVIDENCE_PATH_KEY] = evidence
    return receipt


def _env_failure_receipt(evidence: str) -> dict:
    """No exit code + an execution detail: ``classify_receipt`` -> env_failure."""
    return _receipt(
        None, detail="cannot run: interpreter not found", evidence=evidence,
    )


def _env_command_payload(
    command: str, *, exit_code: int | None = None, detail: str = "no interpreter",
) -> dict:
    """A run_command payload the receipt writer can persist verbatim."""
    return {
        "kind": "verification_command",
        "command": command,
        "env": "",
        "cwd": "/tmp/wt",
        "placeholders": {"checkout": "/tmp/wt", "project": "/tmp/p"},
        "argv": [command],
        "env_overrides": {},
        "assertions": [],
        "exit_code": exit_code,
        "duration_s": 0.1,
        "stdout_tail": "",
        "stderr_tail": "",
        "log_path": None,
        "parity": "absolute",
        "detail": detail,
        "git": {
            "checkout_head": None,
            "baseline_head": None,
            "changed_files_fingerprint": None,
        },
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

    def fake_gate(run, contract, entry, *, invocation_id=None):
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


def test_env_only_set_offers_a_gate_rerun_before_waiver_or_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two environment failures are agent-unfixable but engine-retryable: the
    operator repairs the environment and the engine re-executes exactly these
    gates. The record must address that rerun — one ``receipt_evidence``-bearing
    identity per blocking command."""
    contract = _contract()
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_env_failure_receipt("verification/scheduled/lint-1.json")],
        "typecheck": [
            _env_failure_receipt("verification/scheduled/typecheck-1.json"),
        ],
        "vitest": [_receipt(0)],
    })
    critiques = _patch_repair(monkeypatch)

    outcome = gate_repair.run_post_implement_gate_repair(run, object(), object())

    assert outcome.paused and outcome.rounds == 0
    assert critiques == []  # agent-unfixable: no repair round burned
    signal = run.state.phase_handoff_request
    assert [f["failure_kind"] for f in signal.artifacts["findings"]] == [
        "env_failure", "env_failure",
    ]
    assert signal.available_actions == (
        "retry_verification", "continue_with_waiver", "halt",
    )
    assert signal.artifacts["gate_identities"] == [
        {
            "command": "lint", "hook": "after_phase", "phase": "implement",
            "receipt_evidence": "verification/scheduled/lint-1.json",
        },
        {
            "command": "typecheck", "hook": "after_phase", "phase": "implement",
            "receipt_evidence": "verification/scheduled/typecheck-1.json",
        },
    ]
    # The primary stays a bare triple — waiver identity and route
    # classification are single-identity contracts over the whole mapping.
    assert signal.artifacts["gate_identity"] == {
        "command": "lint", "hook": "after_phase", "phase": "implement",
    }


def test_a_pause_raised_inside_a_loop_records_where_the_round_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload has to say which round and member the gates ran for.

    A ``retry_verification`` resume continues the loop *after* the raising
    phase, and the payload's own ``round`` / ``round_extras_key`` name the
    repair loop by convention — not necessarily the loop this phase belongs to.
    The runner stamps the active loop into ``state.extras`` for exactly as long
    as the loop runs, so the pause captures it while it is still true.
    """
    contract = _contract()
    run = _run(contract)
    from pipeline.runtime.runner import (
        mark_loop_member_executed,
        stamp_active_loop,
    )

    run.state.extras["implement_round"] = 2
    stamp_active_loop(
        run.state,
        loop_key="implement_round",
        phases=("implement", "verify_changes"),
        # An operator-granted extra round: the effective budget, not the
        # declared one, is what a later resume has to accept.
        budget=2,
        until="verify_changes.approved",
    )
    mark_loop_member_executed(run.state, "implement")
    _patch_gates(monkeypatch, {
        "lint": [_env_failure_receipt("verification/scheduled/lint-1.json")],
        "typecheck": [_receipt(0)],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    signal = run.state.phase_handoff_request
    assert signal.artifacts[gate_handoff_actions.LOOP_POSITION_KEY] == {
        "loop_key": "implement_round",
        "loop_phases": ["implement", "verify_changes"],
        "round": 2,
        "phase": "implement",
        "budget": 2,
        # Recorded now, while the round's verdict is still readable: a rebuilt
        # state cannot answer whether this round closed the loop.
        "until_satisfied": False,
        # In execution order — the member still owed by this round is whatever
        # is missing from here, not whatever follows in the declaration — plus
        # the dispatcher that produced it, so the order can be checked against
        # the shapes that dispatcher can actually make.
        "executed": ["implement"],
        "mode": "declared_order",
        # The gate reported on a member that had run, not on one it guards.
        "hook": "after_phase",
    }


def test_a_top_level_pause_records_no_loop_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to position: the phase is not a member of any active loop."""
    contract = _contract()
    run = _run(contract)
    from pipeline.runtime.runner import stamp_active_loop

    # A stale stamp from a loop that already closed must not be read as this
    # phase's position — the phase is not one of its members.
    run.state.extras["plan_round"] = 1
    stamp_active_loop(
        run.state,
        loop_key="plan_round",
        phases=("plan", "validate_plan"),
        budget=1,
        until="validate_plan.approved",
    )
    _patch_gates(monkeypatch, {
        "lint": [_env_failure_receipt("verification/scheduled/lint-1.json")],
        "typecheck": [_receipt(0)],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    signal = run.state.phase_handoff_request
    assert gate_handoff_actions.LOOP_POSITION_KEY not in signal.artifacts


def test_real_execution_stamps_receipt_evidence_onto_the_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """The pointer is not test scaffolding: driving the real persistence seam
    (``run_command`` patched, ``_persist_gate_receipt`` live) shows an
    after_phase env failure reaching the handoff with a ``receipt_evidence``
    path that resolves to the failing receipt on disk."""
    import json

    import pipeline.verification_command as vc

    contract = _contract(schedule=[{
        "after_phase": "implement", "policy": "require",
        "action": "handoff", "commands": ["lint", "typecheck"],
    }])
    run = _run(contract)
    run.state.output_dir = tmp_path
    monkeypatch.setattr(
        vc, "run_command", lambda command, *a, **k: _env_command_payload(command),
    )
    monkeypatch.setattr(
        gate_repair,
        "_classify_gate_receipt",
        lambda receipt, _ctx: classify_receipt(receipt),
    )
    _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    signal = run.state.phase_handoff_request
    assert signal.available_actions == (
        "retry_verification", "continue_with_waiver", "halt",
    )
    identities = signal.artifacts["gate_identities"]
    assert [entry["command"] for entry in identities] == ["lint", "typecheck"]
    for entry in identities:
        evidence = tmp_path / entry["receipt_evidence"]
        assert evidence.is_file()
        recorded = json.loads(evidence.read_text())
        assert recorded["command"] == entry["command"]
        assert recorded["exit_code"] is None


def test_env_set_without_receipt_evidence_gets_no_rerun_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: an env-only set whose executions left no evidence pointer
    cannot prove which receipts a rerun would replace, so the menu stays
    waiver / halt."""
    contract = _contract()
    run = _run(contract)
    _patch_gates(monkeypatch, {
        "lint": [_receipt(None, detail="cannot run")],
        "typecheck": [_receipt(None, detail="cannot run")],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    signal = run.state.phase_handoff_request
    assert signal.available_actions == ("continue_with_waiver", "halt")
    for entry in signal.artifacts["gate_identities"]:
        assert "receipt_evidence" not in entry


def test_mixed_env_and_timeout_set_gets_no_rerun_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout is agent-unfixable too, but re-running it is not the fix —
    the budget is declared in the contract. A set that mixes the two is not
    engine-retryable."""
    contract = _contract()
    run = _run(contract)
    timeout = _receipt(None, evidence="verification/scheduled/typecheck-1.json")
    timeout["outcome"] = "timeout"
    _patch_gates(monkeypatch, {
        "lint": [_env_failure_receipt("verification/scheduled/lint-1.json")],
        "typecheck": [timeout],
        "vitest": [_receipt(0)],
    })
    _patch_repair(monkeypatch)

    gate_repair.run_post_implement_gate_repair(run, object(), object())

    signal = run.state.phase_handoff_request
    assert [f["failure_kind"] for f in signal.artifacts["findings"]] == [
        "env_failure", "timeout",
    ]
    assert signal.available_actions == ("continue_with_waiver", "halt")


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
