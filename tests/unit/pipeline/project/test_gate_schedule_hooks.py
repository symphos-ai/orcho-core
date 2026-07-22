"""Stage 4 hook x policy x action routing over the ScheduledGatePlan (T5).

Drives ``gate_repair.run_gate_hook`` directly with a duck-typed run; subprocess
and FSM boundaries are monkeypatched. Covers the deterministic repair_loop-by-
hook matrix (only after_phase(implement) repairs; everything else degrades to a
logged handoff fallback), per-action routing, manual_only non-planning,
non-require policies never blocking, and the before_delivery blocking rule.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.evidence.verification_receipt import subject_identity
from pipeline.plugins import PluginConfig
from pipeline.project import gate_repair
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_failure import classify_receipt


def test_scheduled_gate_lifecycle_events_carry_full_identity(monkeypatch) -> None:
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.observability.events.emit",
        lambda kind, **payload: emitted.append((kind, payload)),
    )
    entry = SimpleNamespace(command="test")

    gate_repair._emit_scheduled_gate_start(entry, hook="after_phase", phase="implement")
    gate_repair._emit_scheduled_gate_end(
        entry,
        hook="after_phase",
        phase="implement",
        outcome="passed",
        duration_s=1.0,
    )

    assert emitted == [
        (
            "gate.start",
            {
                "name": "test",
                "gate_kind": "scheduled",
                "command": "test",
                "hook": "after_phase",
                "phase": "implement",
                "ownership": "engine",
            },
        ),
        (
            "gate.end",
            {
                "name": "test",
                "outcome": "passed",
                "duration_s": 1.0,
                "command": "test",
                "hook": "after_phase",
                "phase": "implement",
                "ownership": "engine",
            },
        ),
    ]


def test_scheduled_gate_lifecycle_events_carry_project_alias(monkeypatch) -> None:
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.observability.events.emit",
        lambda kind, **payload: emitted.append((kind, payload)),
    )
    entry = SimpleNamespace(command="test")

    gate_repair._emit_scheduled_gate_start(
        entry, hook="after_phase", phase="implement", project_alias="api"
    )
    gate_repair._emit_scheduled_gate_end(
        entry,
        hook="after_phase",
        phase="implement",
        outcome="passed",
        duration_s=1.0,
        project_alias="api",
    )

    assert [payload["project_alias"] for _, payload in emitted] == ["api", "api"]


def _contract(
    schedule: list,
    *,
    required: tuple[str, ...] = ("test",),
    work_mode: str = "governed",
    commands: dict | None = None,
) -> VerificationContract:
    commands = commands or {"test": {"run": "pytest", "cheap": True}}
    schedule = [
        {
            **entry,
            **({"policy": "require"} if entry.get("action") and "policy" not in entry else {}),
        }
        for entry in schedule
    ]
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode=work_mode,
            verification={
                "commands": commands,
                "required": list(required),
                "gate_sets": {"core": {"commands": list(commands)}},
                "selection": [{"always": ["core"]}],
                "schedule": schedule,
            },
        ),
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


def _receipt(exit_code) -> dict:
    return {
        "schema_version": 3,
        "exit_code": exit_code,
        "stdout_tail": "out",
        "stderr_tail": "err",
        "assertions": [],
        "detail": "",
        "dependencies": [],
        "subject": {
            "status": "available",
            "identity": {
                "version": 1,
                "object_format": "sha1",
                "tree_oid": "a" * 40,
                "observed_head_oid": "b" * 40,
                "baseline_oid": None,
            },
        },
    }


def _patch_gate(monkeypatch, results: list[dict]) -> dict:
    calls = {"gate": 0, "repair": 0}
    queue = list(results)

    def fake_gate(run, contract, entry):
        calls["gate"] += 1
        return queue.pop(0) if queue else results[-1]

    monkeypatch.setattr(gate_repair, "_run_gate_command", fake_gate)
    monkeypatch.setattr(
        gate_repair,
        "_classify_gate_receipt",
        lambda receipt, _ctx: classify_receipt(
            receipt,
            current_subject=subject_identity(receipt.get("subject")),
        ),
    )
    return calls


def _patch_repair(monkeypatch, calls: dict) -> None:
    monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: object())

    def fake_dispatch(run, repair_step, ctx, *, round_n, max_rounds):
        calls["repair"] += 1

    monkeypatch.setattr(gate_repair, "_dispatch_repair", fake_dispatch)


# ── repair_loop-by-hook matrix ──────────────────────────────────────────────


def test_repair_loop_after_phase_implement_repairs(monkeypatch) -> None:
    contract = _contract(
        [{"after_phase": "implement", "action": "repair_loop", "commands": ["test"]}],
    )
    run = _run(contract)
    calls = _patch_gate(monkeypatch, [_receipt(1), _receipt(0)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="after_phase",
        phase="implement",
    )
    assert outcome.passed and calls["repair"] == 1
    # the repair flow ran; no handoff fallback note recorded.
    assert "gate_repair_notes" not in run.state.extras


def test_repair_loop_before_phase_degrades_to_handoff(monkeypatch) -> None:
    contract = _contract(
        [{"before_phase": "implement", "action": "repair_loop", "commands": ["test"]}],
    )
    run = _run(contract)
    calls = _patch_gate(monkeypatch, [_receipt(1)])
    _patch_repair(monkeypatch, calls)

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="before_phase",
        phase="implement",
    )
    assert outcome.paused
    assert calls["repair"] == 0
    notes = run.state.extras.get("gate_repair_notes", [])
    assert any(n["kind"] == "repair_loop_fallback" for n in notes)


def test_repair_loop_before_delivery_degrades_to_handoff(monkeypatch) -> None:
    contract = _contract(
        [{"before_delivery": True, "action": "repair_loop", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="before_delivery",
    )
    assert outcome.paused
    notes = run.state.extras.get("gate_repair_notes", [])
    assert any(n["kind"] == "repair_loop_fallback" for n in notes)


def test_repair_loop_on_resume_degrades_to_handoff(monkeypatch) -> None:
    contract = _contract(
        [{"on_resume": True, "action": "repair_loop", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="on_resume")
    assert outcome.paused
    notes = run.state.extras.get("gate_repair_notes", [])
    assert any(n["kind"] == "repair_loop_fallback" for n in notes)


# ── per-action routing ──────────────────────────────────────────────────────


def test_continue_warn_does_not_block(monkeypatch) -> None:
    contract = _contract(
        [{"after_phase": "implement", "action": "continue_warn", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="after_phase",
        phase="implement",
    )
    assert outcome.active and outcome.passed
    assert run.state.phase_handoff_request is None
    assert run.state.halt is False


def test_handoff_action_pauses(monkeypatch) -> None:
    contract = _contract(
        [{"after_phase": "implement", "action": "handoff", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="after_phase",
        phase="implement",
    )
    assert outcome.paused
    assert run.state.phase_handoff_request is not None


def test_abort_action_halts(monkeypatch) -> None:
    contract = _contract(
        [{"after_phase": "implement", "action": "abort", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="after_phase",
        phase="implement",
    )
    assert outcome.halted
    assert run.session.get("status") == "halted"


# ── hook semantics ──────────────────────────────────────────────────────────


def test_manual_only_is_not_planned(monkeypatch) -> None:
    contract = _contract(
        [{"after_phase": "implement", "commands": ["test"]}],
    )
    run = _run(contract)
    calls = _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="manual_only")
    assert outcome.active is False
    assert calls["gate"] == 0


def test_non_require_policy_never_blocks(monkeypatch) -> None:
    # command not required + governed => effective policy 'warn' => filtered out
    # of routing entirely (no command executed, no block).
    contract = _contract(
        [{"after_phase": "implement", "commands": ["test"]}],
        required=(),
    )
    run = _run(contract)
    calls = _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="after_phase",
        phase="implement",
    )
    assert outcome.active is False
    assert calls["gate"] == 0


def test_explicit_manual_policy_never_blocks(monkeypatch) -> None:
    contract = _contract(
        [{"after_phase": "implement", "policy": "manual", "commands": ["test"]}],
    )
    run = _run(contract)
    calls = _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="after_phase",
        phase="implement",
    )
    assert outcome.active is False
    assert calls["gate"] == 0


# ── before_delivery blocking rule ───────────────────────────────────────────


def test_before_delivery_blocks_on_failed_required_receipt(monkeypatch) -> None:
    contract = _contract(
        [{"before_delivery": True, "policy": "require", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])  # failed receipt

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="before_delivery",
    )
    # governed before_delivery derives action=handoff -> pause (blocks delivery).
    assert outcome.paused
    assert run.state.phase_handoff_request is not None


def test_before_delivery_passes_clean_receipt(monkeypatch) -> None:
    contract = _contract(
        [{"before_delivery": True, "policy": "require", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(0)])  # passing receipt

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="before_delivery",
    )
    assert outcome.active and outcome.passed
    assert run.state.phase_handoff_request is None


def test_before_delivery_warn_policy_does_not_block(monkeypatch) -> None:
    contract = _contract(
        [{"before_delivery": True, "policy": "warn", "commands": ["test"]}],
        work_mode="pro",
    )
    run = _run(contract)
    calls = _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(
        run,
        object(),
        object(),
        hook="before_delivery",
    )
    assert outcome.active and outcome.passed
    assert calls["gate"] == 1


def test_on_resume_routes_required_gate(monkeypatch) -> None:
    contract = _contract(
        [{"on_resume": True, "policy": "require", "action": "handoff", "commands": ["test"]}],
    )
    run = _run(contract)
    _patch_gate(monkeypatch, [_receipt(1)])

    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="on_resume")
    assert outcome.paused
    assert run.state.phase_handoff_request is not None


# ── F4: phase is part of the resolved gate identity ─────────────────────────


class TestPhaseIdentity:
    def test_same_command_two_before_phases_does_not_collapse(self) -> None:
        from pipeline.verification_selection import (
            SelectionContext,
            build_scheduled_gate_plan,
        )

        contract = _contract(
            [
                {"before_phase": "plan", "action": "handoff", "commands": ["test"]},
                {"before_phase": "implement", "action": "abort", "commands": ["test"]},
            ],
        )
        plan = build_scheduled_gate_plan(
            contract,
            SelectionContext(work_mode="governed"),
        )
        before = {(e.phase, e.action) for e in plan.entries if e.hook == "before_phase"}
        # two distinct phase-scoped entries, each keeping its own action.
        assert before == {("plan", "handoff"), ("implement", "abort")}

    def test_gates_for_hook_filters_by_entry_phase(self, monkeypatch) -> None:
        contract = _contract(
            [
                {"before_phase": "plan", "action": "handoff", "commands": ["test"]},
                {"before_phase": "implement", "action": "abort", "commands": ["test"]},
            ],
        )
        run = _run(contract)
        _patch_gate(monkeypatch, [_receipt(1)])
        # routing the implement before_phase gate aborts (its own action),
        # not the plan one's handoff.
        outcome = gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook="before_phase",
            phase="implement",
        )
        assert outcome.halted
        assert run.session.get("status") == "halted"


# ── F2: epoch-keyed routing plans; pre-implement never reused post-implement ─


class TestRoutingPlanLifecycle:
    def test_one_plan_per_epoch_across_hooks(self, monkeypatch) -> None:
        import pipeline.project.verification_ledger_runtime as ledger_runtime

        contract = _contract(
            [{"after_phase": "implement", "commands": ["test"]}],
        )
        run = _run(contract)
        _patch_gate(monkeypatch, [_receipt(0)])

        builds = {"n": 0}
        real_build = ledger_runtime.build_scheduled_gate_plan

        def counting_build(c, ctx):
            builds["n"] += 1
            return real_build(c, ctx)

        monkeypatch.setattr(ledger_runtime, "build_scheduled_gate_plan", counting_build)

        # Each distinct hook:phase position is its own epoch -> three builds.
        for hook, phase in (
            ("before_phase", "implement"),
            ("after_phase", "implement"),
            ("before_delivery", ""),
        ):
            gate_repair.run_gate_hook(run, object(), object(), hook=hook, phase=phase)
        assert builds["n"] == 3
        plans = run.state._verification_ledger_epoch_cache
        assert set(plans) == {
            "before_phase:implement",
            "after_phase:implement",
            "before_delivery:",
        }

    def test_same_epoch_reuses_cached_plan(self, monkeypatch) -> None:
        contract = _contract(
            [{"after_phase": "implement", "commands": ["test"]}],
        )
        run = _run(contract)
        _patch_gate(monkeypatch, [_receipt(0), _receipt(0)])
        gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook="after_phase",
            phase="implement",
        )
        plans = run.state._verification_ledger_epoch_cache
        cached = plans["after_phase:implement"]
        run.state.extras["verification_task_kind"] = "something-else"
        gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook="after_phase",
            phase="implement",
        )
        # same hook:phase position -> same cached plan reused (deterministic).
        assert plans["after_phase:implement"] is cached

    @staticmethod
    def _path_gate_contract() -> VerificationContract:
        contract = VerificationContract.from_plugin(
            PluginConfig(
                work_mode="governed",
                verification={
                    "commands": {"lint": {"run": "ruff"}, "mcp": {"run": "m"}},
                    "required": ["lint", "mcp"],
                    "gate_sets": {
                        "baseline": {"commands": ["lint"]},
                        "mcp": {"commands": ["mcp"]},
                    },
                    "selection": [
                        {"always": ["baseline"]},
                        {"paths": ["src/orcho_mcp/**"], "include": ["mcp"]},
                    ],
                    "schedule": [
                        {"before_phase": "implement", "commands": ["lint"]},
                        {"after_phase": "implement", "commands": ["mcp"]},
                    ],
                },
            ),
        )
        assert contract is not None
        return contract

    def _run_path_gate_lifecycle(self, monkeypatch, *, early_hook, early_phase):
        """Drive an early pre-implement routing hook, then implement touches a
        subsystem path, then after_phase(implement) — returns the run."""
        from pipeline.verification_selection import SelectionContext

        run = _run(self._path_gate_contract())
        changed = {"paths": ()}
        monkeypatch.setattr(
            "pipeline.project.verification_selection_context.selection_context_for_run",
            lambda run, contract: SelectionContext(
                work_mode="governed",
                touched_paths=changed["paths"],
            ),
        )
        # lint passes (early hook), mcp fails (after_phase implement) when run.
        monkeypatch.setattr(
            gate_repair,
            "_run_gate_command",
            lambda run, contract, entry: {
                "exit_code": 0 if entry.command == "lint" else 1,
            },
        )
        monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: None)

        # 1) early hook before implement: its own epoch, no mcp gate.
        gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook=early_hook,
            phase=early_phase,
        )
        # 2) implement touches the subsystem path.
        changed["paths"] = ("src/orcho_mcp/a.py",)
        # 3) after_phase(implement) builds its own post-implement plan + routes mcp.
        outcome = gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook="after_phase",
            phase="implement",
        )
        return run, outcome

    def test_before_phase_plan_not_reused_for_after_phase_path_gate(
        self,
        monkeypatch,
    ) -> None:
        run, outcome = self._run_path_gate_lifecycle(
            monkeypatch,
            early_hook="before_phase",
            early_phase="implement",
        )
        plans = run.state._verification_ledger_epoch_cache
        assert "mcp" not in plans["before_phase:implement"].selected_gate_sets
        assert outcome.active and outcome.paused
        assert "mcp" in plans["after_phase:implement"].selected_gate_sets
        assert plans["before_phase:implement"] is not plans["after_phase:implement"]

    def test_after_phase_plan_phase_not_reused_for_after_phase_implement(
        self,
        monkeypatch,
    ) -> None:
        # The reviewer's case: an early after_phase(plan) must not freeze the
        # plan reused by after_phase(implement).
        run, outcome = self._run_path_gate_lifecycle(
            monkeypatch,
            early_hook="after_phase",
            early_phase="plan",
        )
        plans = run.state._verification_ledger_epoch_cache
        assert "mcp" not in plans["after_phase:plan"].selected_gate_sets
        assert outcome.active and outcome.paused
        assert "mcp" in plans["after_phase:implement"].selected_gate_sets
        assert plans["after_phase:plan"] is not plans["after_phase:implement"]


# ── Routing threads task_kind / operator_sets from state.extras ─────────────


class TestSelectionInputsThreaded:
    @staticmethod
    def _task_kind_contract(*, declare_kind: bool) -> VerificationContract:
        verification = {
            "commands": {"bug": {"run": "pytest bug"}},
            "required": ["bug"],
            "gate_sets": {"bugfix": {"commands": ["bug"]}},
            "selection": [{"task_kind": "bugfix", "include": ["bugfix"]}],
            "schedule": [
                {
                    "after_phase": "implement",
                    "gate_sets": ["bugfix"],
                    "policy": "require",
                    "action": "repair_loop",
                }
            ],
        }
        if declare_kind:
            verification["task_kind"] = "bugfix"  # declared production source
        contract = VerificationContract.from_plugin(
            PluginConfig(work_mode="governed", verification=verification),
        )
        assert contract is not None
        return contract

    def test_task_kind_gate_omitted_without_declaration(self, monkeypatch) -> None:
        run = _run(self._task_kind_contract(declare_kind=False))
        calls = _patch_gate(monkeypatch, [_receipt(1)])
        # contract declares no task_kind and no extras -> rule does not match.
        outcome = gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook="after_phase",
            phase="implement",
        )
        assert outcome.active is False
        assert calls["gate"] == 0

    def test_contract_declared_task_kind_reaches_routing(self, monkeypatch) -> None:
        # Production source: task_kind declared ON THE CONTRACT, no test extras.
        run = _run(self._task_kind_contract(declare_kind=True))
        _patch_gate(monkeypatch, [_receipt(1)])
        monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: None)
        outcome = gate_repair.run_gate_hook(
            run,
            object(),
            object(),
            hook="after_phase",
            phase="implement",
        )
        assert outcome.active is True
        assert outcome.paused  # repair_loop with no repair_step -> handoff fallback

    def test_extras_override_task_kind_wins(self, monkeypatch) -> None:
        # The per-run extras override takes priority over the contract default.
        run = _run(self._task_kind_contract(declare_kind=False))
        run.state.extras["verification_task_kind"] = "bugfix"
        _patch_gate(monkeypatch, [_receipt(1)])
        monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: None)
        assert (
            gate_repair.run_gate_hook(
                run,
                object(),
                object(),
                hook="after_phase",
                phase="implement",
            ).active
            is True
        )

    @staticmethod
    def _operator_contract(*, declare_request: bool) -> VerificationContract:
        verification = {
            "commands": {"par": {"run": "p"}},
            "required": ["par"],
            "gate_sets": {"expensive": {"commands": ["par"]}},
            "selection": [{"operator": ["expensive"]}],
            "schedule": [
                {
                    "after_phase": "implement",
                    "gate_sets": ["expensive"],
                    "policy": "require",
                    "action": "repair_loop",
                }
            ],
        }
        if declare_request:
            verification["operator_sets"] = ["expensive"]  # declared opt-in
        contract = VerificationContract.from_plugin(
            PluginConfig(work_mode="governed", verification=verification),
        )
        assert contract is not None
        return contract

    def test_operator_gate_not_selected_without_request(self, monkeypatch) -> None:
        run = _run(self._operator_contract(declare_request=False))
        calls = _patch_gate(monkeypatch, [_receipt(1)])
        assert (
            gate_repair.run_gate_hook(
                run,
                object(),
                object(),
                hook="after_phase",
                phase="implement",
            ).active
            is False
        )
        assert calls["gate"] == 0

    def test_operator_gate_selected_when_contract_requests(self, monkeypatch) -> None:
        # Production source: operator_sets declared ON THE CONTRACT, no extras.
        run = _run(self._operator_contract(declare_request=True))
        _patch_gate(monkeypatch, [_receipt(1)])
        monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: None)
        assert (
            gate_repair.run_gate_hook(
                run,
                object(),
                object(),
                hook="after_phase",
                phase="implement",
            ).active
            is True
        )


# ── F1 / F2: hooks fire at the right place in REAL run_profile dispatch ──────


class TestRealDispatchWiring:
    """Drive a real ``run_profile`` with the gate hooks wired exactly as
    ``dispatch_via_v2_profile`` wires them (``on_phase_pre`` /
    ``on_phase_end``), proving the hooks fire in the right place — not only via
    direct ``run_gate_hook`` calls."""

    @staticmethod
    def _wire(contract, profile, ctx, calls):
        from types import SimpleNamespace

        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState

        state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
        state.extras["verification_contract"] = contract
        state.extras["verification_placeholders"] = PlaceholderContext(checkout="")
        run = SimpleNamespace(
            state=state,
            session={},
            max_rounds=1,
            _gate_profile=profile,
            _gate_ctx=ctx,
            _in_gate_hook=False,
        )

        def on_start(name, st):
            pass

        def on_end(name, st):
            gate_repair.evaluate_post_phase_gates(run, name)

        def on_pre(name, st):
            gate_repair.evaluate_pre_phase_gates(run, name)

        run._on_phase_start = on_start
        run._on_phase_end = on_end
        return run, state, on_start, on_end, on_pre

    def test_before_phase_abort_prevents_handler(self, monkeypatch) -> None:
        from pipeline.runtime import (
            PhaseRegistry,
            PhaseStep,
            Profile,
            run_profile,
        )

        ran: list[str] = []
        reg = PhaseRegistry()
        reg.register("implement", lambda st: (ran.append("implement"), st)[1])
        profile = Profile(
            name="p",
            kind="custom",
            steps=(PhaseStep(phase="implement"),),
        )
        contract = _contract(
            [{"before_phase": "implement", "action": "abort", "commands": ["test"]}],
        )
        run, state, on_start, on_end, on_pre = self._wire(
            contract,
            profile,
            None,
            {},
        )
        monkeypatch.setattr(gate_repair, "_run_gate_command", lambda *a, **k: _receipt(1))

        run_profile(
            profile,
            state,
            reg,
            on_phase_start=on_start,
            on_phase_end=on_end,
            on_phase_pre=on_pre,
        )
        # before_phase abort fired BEFORE the handler — implement never ran.
        assert ran == []
        assert state.halt is True

    def test_after_phase_implement_repairs_before_review(self, monkeypatch) -> None:
        from pipeline.lifecycle import default_lifecycle_context
        from pipeline.runtime import (
            LoopStep,
            PhaseRegistry,
            PhaseStep,
            Profile,
            run_profile,
        )

        ran: list[str] = []
        reg = PhaseRegistry()
        for name in ("implement", "review_changes", "repair_changes"):
            reg.register(name, (lambda n: lambda st: (ran.append(n), st)[1])(name))
        profile = Profile(
            name="p",
            kind="custom",
            steps=(
                PhaseStep(phase="implement"),
                LoopStep(
                    steps=(
                        PhaseStep(phase="review_changes"),
                        PhaseStep(phase="repair_changes"),
                    ),
                    until="review_changes.clean",
                    max_rounds=1,
                    round_extras_key="repair_round",
                ),
            ),
        )
        ctx = default_lifecycle_context(phase_registry=reg)
        contract = _contract(
            [{"after_phase": "implement", "action": "repair_loop", "commands": ["test"]}],
        )
        run, state, on_start, on_end, on_pre = self._wire(
            contract,
            profile,
            ctx,
            {},
        )
        # gate fails once, passes on the re-check after one repair round.
        receipts = iter([_receipt(1), _receipt(0)])
        monkeypatch.setattr(gate_repair, "_run_gate_command", lambda *a, **k: next(receipts))
        monkeypatch.setattr(
            gate_repair,
            "_classify_gate_receipt",
            lambda receipt, _ctx: classify_receipt(
                receipt,
                current_subject=subject_identity(receipt.get("subject")),
            ),
        )

        run_profile(
            profile,
            state,
            reg,
            ctx=ctx,
            on_phase_start=on_start,
            on_phase_end=on_end,
            on_phase_pre=on_pre,
        )
        # repair_changes was dispatched by the gate BEFORE review_changes ran.
        assert "repair_changes" in ran
        assert ran.index("repair_changes") < ran.index("review_changes")
        assert ran[0] == "implement"
