"""``PhaseStep.quality_gates`` declared on profile JSON
fire from ``pipeline.runtime._dispatch_one`` after the handler
returns. Pin the new dispatch contract.

 ``tests`` gate also fires from runtime. Provider
override is now carried by ``LifecycleContext.provider`` in production
(with a state.extras fallback for direct unit tests).

 prep: tests use the ``quality_gate_registry=...`` keyword
argument on ``run_profile`` ( prep DI) instead of the old
``pipeline.quality_gates.default_quality_gate_registry`` monkey-patch
pattern. ``LifecycleContext`` will replace this kwarg
with a typed context field.
"""
from __future__ import annotations

from typing import Any

from pipeline.plugins import PluginConfig
from pipeline.quality_gates import (
    QualityGateRegistry,
    QualityGateResult,
)
from pipeline.runtime import (
    FailStrategy,
    GateKind,
    PhaseRegistry,
    PhaseStep,
    PipelineState,
    Profile,
    QualityGate,
    run_profile,
)


def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


def _stub_handler(name: str, *, mark_skipped: bool = False):
    def _h(state: PipelineState) -> PipelineState:
        entry: dict[str, Any] = {"output": f"{name}-output"}
        if mark_skipped:
            entry["skipped"] = "test reason"
        state.phase_log[name] = entry
        return state
    return _h


class _StubGate:
    """Minimal QualityGateHandler that records invocation + returns
 a configurable ``QualityGateResult``."""

    def __init__(self, *, passed: bool = True, output: str = "ok") -> None:
        self.calls: list[tuple[str, str]] = []  # (gate_name, cwd)
        self._passed = passed
        self._output = output

    def execute(self, gate, state, cwd) -> QualityGateResult:
        self.calls.append((gate.name, cwd))
        return QualityGateResult(
            name=gate.name,
            passed=self._passed,
            output=self._output,
            duration_s=0.0,
            kind=gate.kind,
        )


# ── Gate fires from runtime when declared on PhaseStep ───────────────────────

class TestPhaseStepGateFiresFromRuntime:
    def test_lint_gate_fires_after_handler(self) -> None:
        """Profile-declared ``lint`` gate fires through dispatch."""
        stub = _StubGate(passed=True, output="lint clean")
        gates = QualityGateRegistry()
        gates.register("lint", stub)
        # Register the handler we'll dispatch.
        reg = PhaseRegistry()
        reg.register("implement", _stub_handler("implement"))

        profile = Profile(
            name="p",
            kind="custom",
            steps=(
                PhaseStep(
                    phase="implement",
                    quality_gates=(
                        QualityGate(
                            name="lint",
                            on_fail=FailStrategy.INFORMATIONAL,
                            kind=GateKind.COMPUTATIONAL,
                        ),
                    ),
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg, quality_gate_registry=gates)

        assert stub.calls == [("lint", "/p")]
        # Gate result persisted to phase_log for audit.
        assert "quality_gates" in state.phase_log["implement"]
        assert state.phase_log["implement"]["quality_gates"]["lint"]["passed"] is True

    def test_tests_gate_fires_from_runtime_after_step_2(self) -> None:
        """built-in ``tests`` gate now fires from
 runtime dispatch. Earlier had
 the runtime skip ``tests`` and let ``_PipelineRun._on_phase_end``
 own it; closes that bridge."""
        stub = _StubGate(passed=True, output="all tests passed")
        gates = QualityGateRegistry()
        gates.register("tests", stub)

        reg = PhaseRegistry()
        reg.register("implement", _stub_handler("implement"))

        profile = Profile(
            name="p",
            kind="custom",
            steps=(
                PhaseStep(
                    phase="implement",
                    quality_gates=(
                        QualityGate(
                            name="tests",
                            on_fail=FailStrategy.FEED_INTO_NEXT,
                            feed_target="last_test_output",
                            kind=GateKind.COMPUTATIONAL,
                        ),
                    ),
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg, quality_gate_registry=gates)

        # ``tests`` gate now fires from runtime.
        assert stub.calls == [("tests", "/p")]
        # Audit record persisted.
        assert "tests" in state.phase_log["implement"]["quality_gates"]
        # Legacy bridge fields populated.
        assert "test_result" in state.phase_log["implement"]
        assert "last_test_result" in state.extras


# ── Skipped handlers don't get gates ─────────────────────────────────────────

class TestSkippedHandlerSkipsGates:
    def test_skipped_phase_does_not_fire_gates(self) -> None:
        """When the handler stuffs ``phase_log[name]["skipped"]`` (e.g.
 review on no-uncommitted, fix on clean review), gates declared on the
 step should NOT fire — mirrors legacy behaviour where skipped
 phases didn't run tests."""
        stub = _StubGate()
        gates = QualityGateRegistry()
        gates.register("lint", stub)

        reg = PhaseRegistry()
        reg.register("implement", _stub_handler("implement", mark_skipped=True))

        profile = Profile(
            name="p",
            kind="custom",
            steps=(
                PhaseStep(
                    phase="implement",
                    quality_gates=(
                        QualityGate(
                            name="lint",
                            on_fail=FailStrategy.INFORMATIONAL,
                            kind=GateKind.COMPUTATIONAL,
                        ),
                    ),
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg, quality_gate_registry=gates)

        assert stub.calls == []  # skipped → no gate


# ── Multiple gates fire in declared order ────────────────────────────────────

class TestMultipleGatesOrder:
    def test_gates_fire_in_declared_order(self) -> None:
        order: list[str] = []

        class _OrderedStub:
            def __init__(self, n: str) -> None:
                self.name = n

            def execute(self, gate, state, cwd) -> QualityGateResult:
                order.append(gate.name)
                return QualityGateResult(
                    name=gate.name, passed=True, output="",
                    duration_s=0.0, kind=gate.kind,
                )

        gates = QualityGateRegistry()
        gates.register("lint", _OrderedStub("lint"))
        gates.register("typecheck", _OrderedStub("typecheck"))

        reg = PhaseRegistry()
        reg.register("implement", _stub_handler("implement"))

        profile = Profile(
            name="p",
            kind="custom",
            steps=(
                PhaseStep(
                    phase="implement",
                    quality_gates=(
                        QualityGate(name="lint",
                                    on_fail=FailStrategy.INFORMATIONAL,
                                    kind=GateKind.COMPUTATIONAL),
                        QualityGate(name="typecheck",
                                    on_fail=FailStrategy.INFORMATIONAL,
                                    kind=GateKind.COMPUTATIONAL),
                    ),
                ),
            ),
        )
        run_profile(profile, _state(), reg, quality_gate_registry=gates)

        assert order == ["lint", "typecheck"]


# ── HALT strategy short-circuits remaining gates ─────────────────────────────

class TestHaltShortCircuits:
    def test_halt_gate_stops_remaining_gates_in_step(self) -> None:
        order: list[str] = []

        class _HaltStub:
            def execute(self, gate, state, cwd) -> QualityGateResult:
                order.append(gate.name)
                return QualityGateResult(
                    name=gate.name, passed=False, output="boom",
                    duration_s=0.0, kind=gate.kind,
                )

        class _NeverFireStub:
            def execute(self, gate, state, cwd) -> QualityGateResult:
                order.append(gate.name)  # MUST NOT fire
                return QualityGateResult(
                    name=gate.name, passed=True, output="",
                    duration_s=0.0, kind=gate.kind,
                )

        gates = QualityGateRegistry()
        gates.register("security", _HaltStub())
        gates.register("after_halt", _NeverFireStub())

        reg = PhaseRegistry()
        reg.register("implement", _stub_handler("implement"))

        profile = Profile(
            name="p",
            kind="custom",
            steps=(
                PhaseStep(
                    phase="implement",
                    quality_gates=(
                        QualityGate(name="security",
                                    on_fail=FailStrategy.HALT,
                                    kind=GateKind.INFERENTIAL),
                        QualityGate(name="after_halt",
                                    on_fail=FailStrategy.INFORMATIONAL,
                                    kind=GateKind.COMPUTATIONAL),
                    ),
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg, quality_gate_registry=gates)

        # security fired, halted state, after_halt MUST NOT have fired.
        assert order == ["security"]
        assert state.halt is True
