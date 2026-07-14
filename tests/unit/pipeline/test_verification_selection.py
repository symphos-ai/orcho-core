"""Unit tests for pipeline/verification_selection.py (Stage 4 gate algebra)."""

from __future__ import annotations

import pytest

from pipeline.plugins import PluginConfig
from pipeline.verification_contract import VerificationContract
from pipeline.verification_selection import (
    OPERATOR_SETS_EXTRAS_KEY,
    TASK_KIND_EXTRAS_KEY,
    SelectionContext,
    build_scheduled_gate_plan,
    derive_effective_action,
    derive_effective_policy,
    selection_context_from_extras,
)


def _contract(**verification) -> VerificationContract:
    contract = VerificationContract.from_plugin(
        PluginConfig(verification=verification),
    )
    assert contract is not None
    return contract


def _entry(plan, command, hook):
    matches = [e for e in plan.entries if e.command == command and e.hook == hook]
    assert len(matches) == 1, f"expected one {command}/{hook}, got {matches}"
    return matches[0]


class TestDeriveEffectivePolicy:
    @pytest.mark.parametrize(
        ("work_mode", "required", "expected"),
        [
            # cost is no longer an input: tier (require/suggest) × work_mode only.
            ("fast", False, "manual"),    # suggest tier relaxes to manual
            ("fast", True, "require"),    # require honored despite cost
            ("pro", False, "suggest"),    # declared tier honored
            ("pro", True, "require"),     # declared tier honored
            ("governed", True, "require"),
            ("governed", False, "suggest"),  # suggest tier honored, not escalated
            ("", False, "suggest"),
        ],
    )
    def test_matrix(self, work_mode, required, expected) -> None:
        assert (
            derive_effective_policy(None, work_mode, required=required)
            == expected
        )

    def test_governed_falls_back_to_base_policy_when_not_required(self) -> None:
        assert (
            derive_effective_policy("suggest", "governed", required=False)
            == "suggest"
        )

    def test_unset_keeps_base_policy(self) -> None:
        assert derive_effective_policy("warn", "", required=False) == "warn"

    @pytest.mark.parametrize(
        ("tier", "mode", "expected"),
        [
            (tier, mode, {"fast": "manual" if tier == "suggest" else tier,
                          "governed": "require" if tier == "warn" else tier,
                          "pro": tier}[mode])
            for tier in ("manual", "suggest", "warn", "require")
            for mode in ("fast", "pro", "governed")
        ],
    )
    def test_exact_declared_tier_mode_matrix(self, tier, mode, expected) -> None:
        assert derive_effective_policy(tier, mode, required=False) == expected


class TestDeriveEffectiveAction:
    def test_implement_after_phase(self) -> None:
        assert derive_effective_action("after_phase", "implement", "pro") == "repair_loop"
        assert derive_effective_action("after_phase", "implement", "governed") == "repair_loop"
        assert derive_effective_action("after_phase", "implement", "fast") == "continue_warn"
        assert derive_effective_action("after_phase", "implement", "") == "continue_warn"

    def test_before_delivery_governed_is_exactly_handoff(self) -> None:
        assert derive_effective_action("before_delivery", "", "governed") == "handoff"
        assert derive_effective_action("before_delivery", "", "pro") == "handoff"
        assert derive_effective_action("before_delivery", "", "fast") == "continue_warn"
        assert derive_effective_action("before_delivery", "", "") == "continue_warn"

    def test_other_combinations_continue_warn(self) -> None:
        assert derive_effective_action("before_phase", "plan", "governed") == "continue_warn"
        assert derive_effective_action("manual_only", "", "governed") == "continue_warn"
        assert derive_effective_action("after_phase", "review_changes", "pro") == "continue_warn"


class TestScheduleAbsenceDerivesPolicy:
    def test_schedule_without_policy_derives_from_base_and_work_mode(self) -> None:
        # gate set default_policy=require, command required+cheap, pro -> require.
        contract = _contract(
            commands={"test": {"run": "pytest", "cheap": True}},
            required=["test"],
            gate_sets={"core": {"commands": ["test"], "default_policy": "require"}},
            selection=[{"always": ["core"]}],
            schedule=[{"after_phase": "implement", "commands": ["test"]}],
        )
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="pro"),
        )
        entry = _entry(plan, "test", "after_phase")
        assert entry.policy == "require"
        assert entry.action == "repair_loop"

    def test_explicit_suggest_is_not_transformed(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest", "cheap": True}},
            required=["test"],
            gate_sets={"core": {"commands": ["test"]}},
            selection=[{"always": ["core"]}],
            schedule=[
                {"after_phase": "implement",
                 "policy": "suggest", "commands": ["test"]},
            ],
        )
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        entry = _entry(plan, "test", "after_phase")
        assert entry.policy == "suggest"


class TestWorkModeChangesEffectivePolicy:
    def _contract_require_default(self) -> VerificationContract:
        return _contract(
            commands={"lint": {"run": "ruff", "cheap": False}},
            gate_sets={"core": {"commands": ["lint"], "default_policy": "require"}},
            selection=[{"always": ["core"]}],
            schedule=[{"after_phase": "implement", "commands": ["lint"]}],
        )

    def test_pro_honors_require_default(self) -> None:
        contract = self._contract_require_default()
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="pro"))
        # pro honors the declared require tier; cost (cheap=False) is irrelevant.
        assert _entry(plan, "lint", "after_phase").policy == "require"

    def test_fast_honors_require_default(self) -> None:
        contract = self._contract_require_default()
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="fast"))
        # fast relaxes only the advisory suggest tier; require is honored despite
        # the gate being expensive (cheap=False).
        assert _entry(plan, "lint", "after_phase").policy == "require"

    def test_governed_not_required_uses_base_default(self) -> None:
        contract = self._contract_require_default()
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        # base policy=require is honored as-is (governed only escalates warn).
        assert _entry(plan, "lint", "after_phase").policy == "require"


class TestBlockingTierIndependentOfCost:
    """ADR 0117 guard tests: cost never changes whether a gate blocks."""

    def test_require_not_cheap_stays_require_in_pro_and_fast(self) -> None:
        # require-tier gate, command explicitly NOT cheap, no explicit
        # schedule.policy -> derived. Must stay blocking (require) in pro AND fast.
        contract = _contract(
            commands={"slow": {"run": "pytest", "cheap": False}},
            required=["slow"],
            gate_sets={
                "core": {
                    "commands": ["slow"],
                    "default_policy": "require",
                    "default_cheap": False,
                },
            },
            selection=[{"always": ["core"]}],
            schedule=[{"after_phase": "implement", "commands": ["slow"]}],
        )
        for work_mode in ("pro", "fast"):
            plan = build_scheduled_gate_plan(
                contract, SelectionContext(work_mode=work_mode),
            )
            assert _entry(plan, "slow", "after_phase").policy == "require", work_mode

    def test_suggest_tier_stays_advisory_regardless_of_cost(self) -> None:
        # suggest-tier gate stays advisory (never require) in pro and fast, with
        # the command cheap or expensive.
        def _build(work_mode: str, cheap: bool) -> str:
            contract = _contract(
                commands={"opt": {"run": "pytest", "cheap": cheap}},
                gate_sets={
                    "extra": {
                        "commands": ["opt"],
                        "default_policy": "suggest",
                        "default_cheap": cheap,
                    },
                },
                selection=[{"always": ["extra"]}],
                schedule=[{"after_phase": "implement", "commands": ["opt"]}],
            )
            plan = build_scheduled_gate_plan(
                contract, SelectionContext(work_mode=work_mode),
            )
            return _entry(plan, "opt", "after_phase").policy

        for work_mode in ("pro", "fast"):
            for cheap in (True, False):
                assert _build(work_mode, cheap) != "require", (work_mode, cheap)


class TestMergeDefaultsAndAttribution:
    def test_command_in_two_sets_merges_max_strictness(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={
                "baseline": {"commands": ["test"], "default_policy": "warn"},
                "subsystem": {"commands": ["test"], "default_policy": "require"},
            },
            selection=[
                {"always": ["baseline"]},
                {"paths": ["src/**"], "include": ["subsystem"]},
            ],
            schedule=[{"after_phase": "implement", "commands": ["test"]}],
        )
        plan = build_scheduled_gate_plan(
            contract,
            SelectionContext(work_mode="governed", touched_paths=("src/a.py",)),
        )
        entry = _entry(plan, "test", "after_phase")
        # merged base policy = require (max of warn, require); governed not
        # required -> base policy wins = require.
        assert entry.policy == "require"
        assert entry.primary_gate_set == "baseline"
        assert set(entry.contributing_gate_sets) == {"baseline", "subsystem"}
        # fixed order: baseline (always) before subsystem (paths).
        assert entry.contributing_gate_sets == ("baseline", "subsystem")

    def test_cost_does_not_affect_policy(self) -> None:
        # required command, no declared default_policy -> tier=require, honored by
        # pro. Cost (default_cheap) is orthogonal: flipping it must not move the
        # effective policy (ADR 0117).
        def _build(default_cheap: bool):
            contract = _contract(
                commands={"test": {"run": "pytest"}},
                required=["test"],
                gate_sets={
                    "a": {"commands": ["test"]},
                    "b": {"commands": ["test"], "default_cheap": default_cheap},
                },
                selection=[{"always": ["a", "b"]}],
                schedule=[{"after_phase": "implement", "commands": ["test"]}],
            )
            plan = build_scheduled_gate_plan(
                contract, SelectionContext(work_mode="pro"),
            )
            return _entry(plan, "test", "after_phase").policy

        # require tier from required-ness, NOT from cost.
        assert _build(default_cheap=True) == "require"
        assert _build(default_cheap=False) == "require"

    def test_schedule_gate_sets_restricts_merge_source(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={
                "baseline": {"commands": ["test"], "default_policy": "warn"},
                "strict": {"commands": ["test"], "default_policy": "require"},
            },
            selection=[{"always": ["baseline", "strict"]}],
            schedule=[
                {"after_phase": "implement",
                 "commands": ["test"], "gate_sets": ["baseline"]},
            ],
        )
        # pro honors the declared tier as-is, so a narrowed base policy of warn
        # stays warn — proving the merge source is restricted to baseline. (Under
        # governed, warn would escalate to require and mask the narrowing.)
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="pro"),
        )
        # merge source narrowed to baseline -> base policy = warn (not require).
        entry = _entry(plan, "test", "after_phase")
        assert entry.policy == "warn"
        # attribution is unaffected by the merge restriction.
        assert set(entry.contributing_gate_sets) == {"baseline", "strict"}


class TestTieBreakerAndManualOnly:
    def test_conflicting_explicit_entries_take_max_strictness(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={"core": {"commands": ["test"]}},
            selection=[{"always": ["core"]}],
            schedule=[
                {"after_phase": "implement",
                 "policy": "warn", "commands": ["test"]},
                {"after_phase": "implement",
                 "policy": "require", "action": "repair_loop", "commands": ["test"]},
            ],
        )
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="fast"))
        entry = _entry(plan, "test", "after_phase")
        assert entry.policy == "require"
        assert entry.action == "repair_loop"

    def test_none_policy_does_not_win_over_explicit(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={"core": {"commands": ["test"]}},
            selection=[{"always": ["core"]}],
            schedule=[
                {"after_phase": "implement", "commands": ["test"]},
                {"after_phase": "implement",
                 "policy": "warn", "commands": ["test"]},
            ],
        )
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        # Every declared tier is mode-projected: governed escalates warn.
        assert _entry(plan, "test", "after_phase").policy == "require"

    def test_command_without_schedule_is_manual_only(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={"core": {"commands": ["test"]}},
            selection=[{"always": ["core"]}],
        )
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="pro"))
        entry = _entry(plan, "test", "manual_only")
        assert entry.policy == "manual"
        assert entry.action == "continue_warn"

    @pytest.mark.parametrize("default_policy", ("warn", "require"))
    def test_unscheduled_command_ignores_gate_set_default_policy(
        self, default_policy: str,
    ) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={
                "core": {
                    "commands": ["test"],
                    "default_policy": default_policy,
                },
            },
            selection=[{"always": ["core"]}],
        )
        entry = _entry(
            build_scheduled_gate_plan(contract, SelectionContext(work_mode="pro")),
            "test",
            "manual_only",
        )
        assert entry.policy == "manual"

    def test_required_unscheduled_command_is_manual_only(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            required=["test"],
            gate_sets={"core": {"commands": ["test"], "default_policy": "require"}},
            selection=[{"always": ["core"]}],
        )
        entry = _entry(
            build_scheduled_gate_plan(contract, SelectionContext(work_mode="governed")),
            "test",
            "manual_only",
        )
        assert (entry.hook, entry.policy) == ("manual_only", "manual")

    def test_direct_schedule_command_is_unconditionally_selected(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}, "lint": {"run": "ruff"}},
            gate_sets={"core": {"commands": ["test"]}},
            selection=[{"always": ["core"]}],
            schedule=[{"after_phase": "implement", "commands": ["lint"]}],
        )
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="pro"))
        entry = _entry(plan, "lint", "after_phase")
        assert entry.activation_binding == "always"
        assert entry.contributing_gate_sets == ()
        # test has no applicable schedule entry -> manual_only.
        assert _entry(plan, "test", "manual_only")


class TestEffectiveActionAlgebra:
    def _delivery_contract(self, **extra_schedule) -> VerificationContract:
        sched = {"before_delivery": True, "policy": "require", "commands": ["test"]}
        sched.update(extra_schedule)
        return _contract(
            commands={"test": {"run": "pytest"}},
            required=["test"],
            gate_sets={"core": {"commands": ["test"]}},
            selection=[{"always": ["core"]}],
            schedule=[sched],
        )

    def test_governed_before_delivery_without_action_is_exactly_handoff(self) -> None:
        contract = self._delivery_contract()
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        entry = _entry(plan, "test", "before_delivery")
        assert entry.action == "handoff"

    def test_explicit_abort_on_before_delivery_is_preserved(self) -> None:
        contract = self._delivery_contract(action="abort")
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        assert _entry(plan, "test", "before_delivery").action == "abort"

    def test_merged_default_action_wins_over_work_mode_derive(self) -> None:
        contract = _contract(
            commands={"test": {"run": "pytest"}},
            gate_sets={
                "core": {
                    "commands": ["test"], "default_policy": "require",
                    "default_action": "handoff",
                },
            },
            selection=[{"always": ["core"]}],
            schedule=[{"after_phase": "implement", "commands": ["test"]}],
        )
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="pro"))
        # work_mode would derive repair_loop, but merged default_action=handoff wins.
        assert _entry(plan, "test", "after_phase").action == "handoff"


class TestOperatorOptIn:
    """``operator`` gate sets are opt-in: declared behind an operator rule but
    selected only when the run explicitly requested them (F2)."""

    def _operator_contract(self) -> VerificationContract:
        return _contract(
            commands={"lint": {"run": "ruff"}, "parity": {"run": "p"}},
            gate_sets={
                "baseline": {"commands": ["lint"]},
                "expensive": {"commands": ["parity"]},
            },
            selection=[
                {"always": ["baseline"]},
                {"operator": ["expensive"]},
            ],
            schedule=[{"after_phase": "implement", "commands": ["parity"]}],
        )

    def test_operator_set_not_selected_without_request(self) -> None:
        plan = build_scheduled_gate_plan(
            self._operator_contract(), SelectionContext(work_mode="governed"),
        )
        # the expensive operator gate is NOT selected by a default run.
        assert "expensive" not in plan.selected_gate_sets
        assert "parity" in plan.selected_commands

    def test_operator_set_selected_when_requested(self) -> None:
        plan = build_scheduled_gate_plan(
            self._operator_contract(),
            SelectionContext(work_mode="governed", operator_sets=("expensive",)),
        )
        assert "expensive" in plan.selected_gate_sets
        assert "parity" in plan.selected_commands


class TestSelectionContextFromExtras:
    @staticmethod
    def _contract_wm(work_mode: str) -> VerificationContract:
        contract = VerificationContract.from_plugin(
            PluginConfig(
                work_mode=work_mode,
                verification={"commands": {"t": {"run": "t"}}},
            ),
        )
        assert contract is not None
        return contract

    def test_reads_task_kind_and_operator_sets(self) -> None:
        contract = self._contract_wm("governed")
        extras = {
            TASK_KIND_EXTRAS_KEY: "bugfix",
            OPERATOR_SETS_EXTRAS_KEY: ["a", "b"],
        }
        ctx = selection_context_from_extras(
            extras, contract, touched_paths=("src/x.py",),
        )
        assert ctx.task_kind == "bugfix"
        assert ctx.operator_sets == ("a", "b")
        assert ctx.touched_paths == ("src/x.py",)
        assert ctx.work_mode == "governed"

    def test_absent_inputs_degrade_to_inert_defaults(self) -> None:
        contract = self._contract_wm("pro")
        ctx = selection_context_from_extras({}, contract)
        assert ctx.task_kind is None
        assert ctx.operator_sets == ()
        assert ctx.work_mode == "pro"

    def test_malformed_inputs_are_ignored(self) -> None:
        contract = self._contract_wm("governed")
        ctx = selection_context_from_extras(
            {TASK_KIND_EXTRAS_KEY: 5, OPERATOR_SETS_EXTRAS_KEY: "nope"},
            contract,
        )
        assert ctx.task_kind is None
        assert ctx.operator_sets == ()


class TestDeterminism:
    def test_plan_is_stable_across_runs(self) -> None:
        contract = _contract(
            commands={"a": {"run": "a"}, "b": {"run": "b"}},
            gate_sets={
                "x": {"commands": ["a", "b"]},
                "y": {"commands": ["b"]},
            },
            selection=[
                {"always": ["x"]},
                {"operator": ["y"]},
            ],
            schedule=[{"after_phase": "implement", "gate_sets": ["x"]}],
        )
        ctx = SelectionContext(work_mode="pro", operator_sets=("y",))
        first = build_scheduled_gate_plan(contract, ctx)
        second = build_scheduled_gate_plan(contract, ctx)
        assert first == second
        assert first.selected_gate_sets == ("x", "y")
        assert first.selected_commands == ("a", "b")
