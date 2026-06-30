"""``LifecycleContext`` + typed Protocols
contract. Pin:

 * ``LifecycleContext`` dataclass field shape ( base + 5e-5
 additions: provider, run_config, plan/git/text helpers,
 session_mode_resolver, test_config_resolver)
 * ``PlanHelpers`` / ``GitHelpers`` / ``TextHelpers`` Protocol
 structural typing — minimal stub matches without inheritance
 * ``default_lifecycle_context()`` factory wires the existing
 ``pipeline.project_orchestrator`` module-level helpers into the
 Protocol fields
 * ``PhaseLifecycle.execute_step`` still raises NotImplementedError
 ( lands the body)

These tests are pure-types / pure-construction. No call sites use
``LifecycleContext`` yet (substeps 2-4 wire it into dispatch).
"""
from __future__ import annotations

import pytest

from pipeline.lifecycle import (
    GitHelpers,
    LifecycleContext,
    PlanHelpers,
    StepOutcome,
    StepStatus,
    TextHelpers,
    default_lifecycle_context,
)
from pipeline.runtime import PhaseRegistry

# ── Protocol structural typing ───────────────────────────────────────────────

class TestProtocolsStructural:
    """Plugin authors construct stubs without inheriting Protocol —
 verify duck-typing works for all 3."""

    def test_plan_helpers_stub_satisfies_protocol(self) -> None:
        class Stub:
            def validate_paths(self, plan, project_dir: str):
                return ([], [])
        assert isinstance(Stub(), PlanHelpers)

    def test_git_helpers_stub_satisfies_protocol(self) -> None:
        class Stub:
            def has_uncommitted(self, cwd: str) -> bool:
                return False
        assert isinstance(Stub(), GitHelpers)

    def test_text_helpers_stub_satisfies_protocol(self) -> None:
        class Stub:
            def critique_is_empty(self, text: str) -> bool:
                return True
        assert isinstance(Stub(), TextHelpers)

    def test_protocol_rejects_missing_method(self) -> None:
        """Stub missing a required method does NOT satisfy Protocol."""
        class Broken:
            pass
        assert not isinstance(Broken(), PlanHelpers)


# ── LifecycleContext field shape ─────────────────────────────────────────────

class TestLifecycleContextShape:
    def test_minimal_construction_only_phase_registry_required(self) -> None:
        reg = PhaseRegistry()
        ctx = LifecycleContext(phase_registry=reg)
        assert ctx.phase_registry is reg
        # All other fields default to None / empty.
        assert ctx.session_adapter_registry is None
        assert ctx.quality_gate_registry is None
        assert ctx.human_review_backend is None
        assert ctx.provider is None
        assert ctx.run_config == {}
        assert ctx.plan_helpers is None
        assert ctx.git_helpers is None
        assert ctx.text_helpers is None
        assert ctx.session_mode_resolver is None
        assert ctx.test_config_resolver is None
        assert ctx.on_event is None
        assert ctx.on_metrics is None
        assert ctx.on_checkpoint is None

    def test_full_construction_all_fields_settable(self) -> None:
        """Customer plugin constructs a custom context with stubs for
 every field — ``orcho.execution_modes`` /
 ``orcho.quality_gates`` plugin authors do this."""
        reg = PhaseRegistry()

        class StubPlan:
            def validate_paths(self, plan, project_dir): return ([], [])

        class StubGit:
            def has_uncommitted(self, cwd): return False

        class StubText:
            def critique_is_empty(self, text): return True

        ctx = LifecycleContext(
            phase_registry=reg,
            session_adapter_registry="sar",
            quality_gate_registry="qgr",
            human_review_backend="hrb",
            provider="prov",
            run_config={"foo": "bar"},
            plan_helpers=StubPlan(),
            git_helpers=StubGit(),
            text_helpers=StubText(),
            session_mode_resolver=lambda **kw: None,
            test_config_resolver=lambda p: None,
            on_event=lambda name, payload: None,
            on_metrics=lambda name, st: None,
            on_checkpoint=lambda name, st: None,
        )
        # Core wiring readable.
        assert ctx.run_config["foo"] == "bar"
        assert ctx.provider == "prov"
        assert isinstance(ctx.plan_helpers, PlanHelpers)
        assert isinstance(ctx.git_helpers, GitHelpers)
        assert isinstance(ctx.text_helpers, TextHelpers)


# ── default_lifecycle_context() factory ──────────────────────────────────────

class TestDefaultLifecycleContext:
    def test_factory_minimal_args_wires_all_helpers(self) -> None:
        reg = PhaseRegistry()
        ctx = default_lifecycle_context(phase_registry=reg)

        assert ctx.phase_registry is reg
        assert ctx.plan_helpers is not None
        assert ctx.git_helpers is not None
        assert ctx.text_helpers is not None
        assert ctx.session_mode_resolver is not None
        assert ctx.test_config_resolver is not None

        # Each helper structurally satisfies its Protocol.
        assert isinstance(ctx.plan_helpers, PlanHelpers)
        assert isinstance(ctx.git_helpers, GitHelpers)
        assert isinstance(ctx.text_helpers, TextHelpers)

    def test_factory_text_helpers_delegate_to_critique_is_empty(self) -> None:
        """text_helpers.critique_is_empty wires through to
 ``project_orchestrator.critique_is_empty``."""
        ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
        assert ctx.text_helpers.critique_is_empty(
            '{"verdict":"APPROVED","short_summary":"ok","findings":[]}'
        ) is True
        assert ctx.text_helpers.critique_is_empty("LGTM") is False
        assert ctx.text_helpers.critique_is_empty("no issues found") is False
        assert ctx.text_helpers.critique_is_empty("found a real bug") is False
        assert ctx.text_helpers.critique_is_empty("") is True  # bare empty

    def test_factory_optional_args_threaded_through(self) -> None:
        ctx = default_lifecycle_context(
            phase_registry=PhaseRegistry(),
            quality_gate_registry="my-gates",
            session_adapter_registry="my-adapters",
            provider="my-provider",
            run_config={"max_rounds": 3},
        )
        assert ctx.quality_gate_registry == "my-gates"
        assert ctx.session_adapter_registry == "my-adapters"
        assert ctx.provider == "my-provider"
        assert ctx.run_config["max_rounds"] == 3

    def test_factory_test_helpers_patchable_via_canonical_module(
        self, monkeypatch,
    ) -> None:
        """Factory binds the git helper to its canonical home in
        ``core.io.git_helpers`` (ADR 0042 Phase J retired the legacy
        ``project_orchestrator.has_uncommitted`` re-export). Tests
        patch the canonical module path and the default GitHelpers
        reads the rebinding.
        """
        from core.io import git_helpers as _git

        monkeypatch.setattr(_git, "has_uncommitted",
                            lambda cwd: True, raising=True)

        ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
        assert ctx.git_helpers.has_uncommitted("/anywhere") is True


# ── PhaseLifecycle.execute_step contract ─────────────────────────────────────
# State-machine transition tests live in test_execute_step.py.


# ── StepOutcome invariants (regression — already pinned in lifecycle.py
#  but a substep-1 commit is a good place to re-pin the contract) ──────────

class TestStepOutcomeInvariantsRegression:
    def test_retry_requested_requires_payload(self) -> None:
        with pytest.raises(ValueError, match="requires retry_payload"):
            StepOutcome(status=StepStatus.RETRY_REQUESTED, state=None)

    def test_halted_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="requires reason"):
            StepOutcome(status=StepStatus.HALTED, state=None)

    def test_completed_does_not_require_reason_or_payload(self) -> None:
        # No-raise smoke — completed is the happy path.
        StepOutcome(status=StepStatus.COMPLETED, state=None)
