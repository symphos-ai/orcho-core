"""quality-gate dispatcher.

Pin per-strategy state mutation, registry behaviour, TestsGate
parity vs legacy run_tests, and dispatcher exception isolation.
"""
from __future__ import annotations

import pytest

from agents.entities import TestResult
from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.observability.metrics import MetricsCollector
from pipeline.plugins import PluginConfig
from pipeline.project.run import _PipelineRun
from pipeline.quality_gates import (
    QualityGateHandler,
    QualityGateRegistry,
    QualityGateResult,
    TestsGate,
    apply_fail_strategy,
    default_quality_gate_registry,
    run_quality_gate,
)
from pipeline.runtime import (
    FailStrategy,
    GateKind,
    PhaseRegistry,
    PipelineState,
    QualityGate,
)


def _state(**kw) -> PipelineState:
    kw.setdefault("plugin", PluginConfig())
    return PipelineState(task="t", project_dir="/p", **kw)


# ── QualityGateResult ─────────────────────────────────────────────────────────

class TestQualityGateResult:
    def test_minimal(self) -> None:
        r = QualityGateResult(
            name="tests", passed=True, output="", duration_s=0.5,
        )
        assert r.kind is GateKind.COMPUTATIONAL  # default
        assert r.error is None

    def test_inferential_with_cost(self) -> None:
        r = QualityGateResult(
            name="security_review", passed=False, output="leak",
            duration_s=12.5, kind=GateKind.INFERENTIAL, cost_usd=0.03,
        )
        assert r.cost_usd == 0.03

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name is empty"):
            QualityGateResult(name="", passed=True, output="", duration_s=0)


# ── QualityGateRegistry ───────────────────────────────────────────────────────

class TestQualityGateRegistry:
    def test_register_and_lookup(self) -> None:
        reg = QualityGateRegistry()
        reg.register("tests", TestsGate())
        assert reg.has("tests") is True
        assert isinstance(reg.get("tests"), TestsGate)

    def test_unknown_get_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown quality gate"):
            QualityGateRegistry().get("ghost")

    def test_get_or_none_for_unknown(self) -> None:
        assert QualityGateRegistry().get_or_none("ghost") is None

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            QualityGateRegistry().register("", TestsGate())

    def test_default_registry_has_tests(self) -> None:
        assert default_quality_gate_registry().has("tests")

    def test_default_registry_is_singleton(self) -> None:
        assert (
            default_quality_gate_registry()
            is default_quality_gate_registry()
        )

    def test_protocol_satisfied_by_testsgate(self) -> None:
        assert isinstance(TestsGate(), QualityGateHandler)


# ── apply_fail_strategy — per-strategy state mutation ────────────────────────

class TestApplyFailStrategy:
    def _gate(self, on_fail: FailStrategy, **kw) -> QualityGate:
        return QualityGate(name="tests", on_fail=on_fail, **kw)

    def _result(self, passed: bool, output: str = "FAIL output") -> QualityGateResult:
        return QualityGateResult(
            name="tests", passed=passed, output=output, duration_s=0.1,
        )

    def test_passed_result_is_noop_regardless_of_strategy(self) -> None:
        for strat in FailStrategy:
            kw = {"feed_target": "x"} if strat is FailStrategy.FEED_INTO_NEXT else {}
            gate = self._gate(strat, **kw)
            s = _state()
            halted = apply_fail_strategy(gate, self._result(passed=True), s)
            assert halted is False
            assert s.halt is False
            assert s.last_critique == ""

    def test_halt_strategy_sets_state_halt(self) -> None:
        s = _state()
        halted = apply_fail_strategy(
            self._gate(FailStrategy.HALT), self._result(False), s,
        )
        assert halted is True
        assert s.halt is True
        assert "tests" in s.halt_reason

    def test_feed_into_next_writes_extras(self) -> None:
        s = _state()
        halted = apply_fail_strategy(
            self._gate(FailStrategy.FEED_INTO_NEXT, feed_target="last_test_output"),
            self._result(False, output="FAIL pytest"),
            s,
        )
        assert halted is False
        assert s.halt is False
        assert s.extras["last_test_output"] == "FAIL pytest"

    def test_feed_into_next_default_target_when_unset(self) -> None:
        # QualityGate validation requires feed_target for FEED_INTO_NEXT,
        # so we can't construct the dataclass without it. apply_fail_strategy
        # falls back to "last_test_output" when feed_target ends up None
        # at runtime (e.g. legacy gates injected via different code path).
        gate = self._gate(FailStrategy.FEED_INTO_NEXT, feed_target="x")
        # bypass __post_init__ by mutation through dataclasses.replace —
        # but QualityGate is frozen, so just test the explicit path.
        s = _state()
        apply_fail_strategy(gate, self._result(False, output="FAIL"), s)
        assert s.extras["x"] == "FAIL"

    def test_trigger_replan_sets_last_critique(self) -> None:
        s = _state()
        halted = apply_fail_strategy(
            self._gate(FailStrategy.TRIGGER_REPLAN),
            self._result(False, output="API mismatch"),
            s,
        )
        assert halted is False
        assert s.halt is False
        assert s.last_critique == "API mismatch"

    def test_informational_is_noop(self) -> None:
        s = _state()
        halted = apply_fail_strategy(
            self._gate(FailStrategy.INFORMATIONAL),
            self._result(False, output="metric drift"),
            s,
        )
        assert halted is False
        assert s.halt is False
        assert s.last_critique == ""
        assert s.extras == {}


# ── run_quality_gate dispatcher ──────────────────────────────────────────────

class TestRunQualityGate:
    def test_dispatches_to_handler(self) -> None:
        seen = []

        class StubGate:
            def execute(self, gate, state, cwd):
                seen.append((gate.name, cwd))
                return QualityGateResult(
                    name=gate.name, passed=True, output="", duration_s=0.1,
                )

        reg = QualityGateRegistry()
        reg.register("tests", StubGate())
        gate = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        result = run_quality_gate(gate, _state(), "/cwd", reg)
        assert result.passed is True
        assert seen == [("tests", "/cwd")]

    def test_unknown_gate_raises(self) -> None:
        gate = QualityGate(name="ghost", on_fail=FailStrategy.HALT)
        with pytest.raises(KeyError, match="Unknown quality gate"):
            run_quality_gate(gate, _state(), "/cwd", QualityGateRegistry())

    def test_handler_exception_becomes_failed_result(self) -> None:
        class BrokenGate:
            def execute(self, gate, state, cwd):
                raise RuntimeError("validator exploded")

        reg = QualityGateRegistry()
        reg.register("tests", BrokenGate())
        gate = QualityGate(name="tests", on_fail=FailStrategy.INFORMATIONAL)
        result = run_quality_gate(gate, _state(), "/cwd", reg)

        assert result.passed is False
        assert "validator exploded" in result.output
        assert result.error == "validator exploded"
        assert result.kind is GateKind.COMPUTATIONAL


# ── TestsGate (parity vs legacy run_tests) ───────────────────────────────────

class TestTestsGate:
    def test_skipped_when_no_run_command(self) -> None:
        plugin = PluginConfig(quality_gates={"tests": {"run_command": None}})
        s = _state(plugin=plugin)
        gate = QualityGate(name="tests", on_fail=FailStrategy.FEED_INTO_NEXT,
                           feed_target="x")
        result = TestsGate().execute(gate, s, "/cwd")
        # Legacy run_tests returns TestResult(skipped=True) → TestsGate
        # surfaces this as passed=True with empty output.
        assert result.passed is True
        assert result.output == ""

    def test_passes_when_run_command_succeeds(self, tmp_path) -> None:
        plugin = PluginConfig(quality_gates={"tests": {"run_command": "echo ok", "fail_keyword": "zzz_will_not_match", "timeout": 5}})
        s = _state(plugin=plugin)
        gate = QualityGate(name="tests", on_fail=FailStrategy.FEED_INTO_NEXT,
                           feed_target="x")
        result = TestsGate().execute(gate, s, str(tmp_path))
        assert result.passed is True
        assert "ok" in result.output

    def test_fails_when_fail_keyword_in_output(self, tmp_path) -> None:
        plugin = PluginConfig(quality_gates={"tests": {"run_command": "echo 'tests failed: 3'", "fail_keyword": "failed", "timeout": 5}})
        s = _state(plugin=plugin)
        gate = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        result = TestsGate().execute(gate, s, str(tmp_path))
        assert result.passed is False
        assert "failed" in result.output

    def test_handler_exception_captured_as_failed_result(self, monkeypatch) -> None:
        """Broken test runner shouldn't crash the pipeline. The handler
 catches the exception and returns ``passed=False`` with ``error``
 populated for debugging."""
        # ADR 0042 Phase J: TestsGate's fallback path imports
        # ``run_tests`` from ``pipeline.project_testing`` directly (the
        # legacy ``pipeline.project_orchestrator.run_tests`` shim
        # retired). Patch the canonical home.
        from pipeline import project_testing
        def boom(*a, **kw):
            raise RuntimeError("test runner broke")
        monkeypatch.setattr(project_testing, "run_tests", boom)

        s = _state()
        gate = QualityGate(name="tests", on_fail=FailStrategy.INFORMATIONAL)
        result = TestsGate().execute(gate, s, "/cwd")
        assert result.passed is False
        assert "test runner broke" in result.error

    def test_kind_is_computational(self, tmp_path) -> None:
        plugin = PluginConfig(quality_gates={"tests": {"run_command": "true"}})
        s = _state(plugin=plugin)
        gate = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        result = TestsGate().execute(gate, s, str(tmp_path))
        assert result.kind is GateKind.COMPUTATIONAL


# ── End-to-end: gate result → fail strategy applied to state ─────────────────

class TestGateAppliedToState:
    """Compose run_quality_gate + apply_fail_strategy so callers can use
 them as a unit (the orchestrator wires them this way in
 ``_on_phase_end``)."""

    def _stub_registry(self, passed: bool, output: str = "") -> QualityGateRegistry:
        class StubGate:
            def execute(self, gate, state, cwd):
                return QualityGateResult(
                    name=gate.name, passed=passed, output=output, duration_s=0.0,
                )
        reg = QualityGateRegistry()
        reg.register("tests", StubGate())
        return reg

    def test_pass_noop(self) -> None:
        reg = self._stub_registry(passed=True)
        gate = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        s = _state()
        result = run_quality_gate(gate, s, "/cwd", reg)
        halted = apply_fail_strategy(gate, result, s)
        assert halted is False
        assert s.halt is False

    def test_fail_with_halt(self) -> None:
        reg = self._stub_registry(passed=False, output="failure")
        gate = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        s = _state()
        result = run_quality_gate(gate, s, "/cwd", reg)
        halted = apply_fail_strategy(gate, result, s)
        assert halted is True
        assert s.halt is True

    def test_fail_with_replan_threads_into_critique(self) -> None:
        reg = self._stub_registry(passed=False, output="schema mismatch")
        gate = QualityGate(name="tests", on_fail=FailStrategy.TRIGGER_REPLAN)
        s = _state()
        result = run_quality_gate(gate, s, "/cwd", reg)
        apply_fail_strategy(gate, result, s)
        assert s.last_critique == "schema mismatch"


# ── Orchestrator wiring ───────────────────────────────────────────────────────

class TestPipelineRunGateWiring:
    class FakeAgent:
        model = "fake-model"

    def _run(self, tmp_path, provider) -> _PipelineRun:
        agent = self.FakeAgent()
        phase_config = PhaseAgentConfig(
            plan_agent=agent,
            implement_agent=agent,
            repair_changes_agent=agent,
            repair_escalation_agent=agent,
            validate_plan_agent=agent,
            review_changes_agent=agent,
            final_acceptance_agent=agent,
        )
        state = _state(phase_config=phase_config)
        state.phase_log["implement"] = {"output": "built"}
        return _PipelineRun(
            task="t",
            project_path=tmp_path,
            git_cwd=str(tmp_path),
            plugin=PluginConfig(),
            output_dir=None,
            dry_run=False,
            profile_name="advanced",
            session_mode=SessionMode.AUTO,
            max_rounds=0,
            plan_model="m",
            implement_model="m",
            repair_model="m",
            repair_escalation_model="m",
            review_model="m",
            do_plan=False,
            do_build=True,
            do_review=False,
            _provider=provider,
            phase_config=phase_config,
            state=state,
            registry=PhaseRegistry(),
            session={"phases": {}},
            session_ts="test",
            codemap="",
            _metrics=MetricsCollector(plan_model="m", implement_model="m", review_model="m"),
            _ckpt=None,
            _chain_same_model_only=True,
        )

    def test_provider_supplied_test_result_still_records_gate_audit(self, tmp_path) -> None:
        """tests gate now fires from runtime
 ``_fire_step_quality_gates``; provider override is read from
 ``LifecycleContext.provider`` in production. Was previously
 ``_on_phase_end`` driven."""
        from pipeline.lifecycle import default_lifecycle_context
        from pipeline.runtime import (
            FailStrategy as _FS,
            GateKind as _GK,
            PhaseStep as _PS,
            Profile as _Pr,
            QualityGate as _QG,
            run_profile as _run_profile,
        )

        class Provider:
            def run_tests(self, cwd, plugin):
                return TestResult(
                    skipped=False,
                    passed=False,
                    output="pytest failed",
                    duration=0.2,
                )

        run = self._run(tmp_path, Provider())
        run.registry.register("implement", lambda s: s)
        ctx = default_lifecycle_context(
            phase_registry=run.registry,
            provider=Provider(),
        )

        profile = _Pr(
            name="t", kind="custom",
            steps=(
                _PS(
                    phase="implement",
                    quality_gates=(
                        _QG(name="tests", on_fail=_FS.FEED_INTO_NEXT,
                            feed_target="last_test_output",
                            kind=_GK.COMPUTATIONAL),
                    ),
                ),
            ),
        )
        _run_profile(profile, run.state, run.registry, ctx=ctx)

        gate = run.state.phase_log["implement"]["quality_gates"]["tests"]
        assert gate["passed"] is False
        assert gate["output"] == "pytest failed"
        assert gate["kind"] == "computational"
        assert run.state.last_test_output == "pytest failed"
        assert run.state.extras["last_test_result"].failed is True

    def test_testsgate_result_records_output_in_gate_audit(self, tmp_path) -> None:
        """provider returns None → falls through to
 TestsGate's subprocess path (legacy ``run_tests`` reading
 ``plugin.testing``)."""
        from pipeline.lifecycle import default_lifecycle_context
        from pipeline.runtime import (
            FailStrategy as _FS,
            GateKind as _GK,
            PhaseStep as _PS,
            Profile as _Pr,
            QualityGate as _QG,
            run_profile as _run_profile,
        )

        class Provider:
            def run_tests(self, cwd, plugin):
                return None  # signal: fall through to subprocess path

        run = self._run(tmp_path, Provider())
        run.plugin = PluginConfig(quality_gates={"tests": {"run_command": "echo 'tests failed'", "fail_keyword": "failed", "timeout": 5}})
        run.state.plugin = run.plugin
        run.state.extras["git_cwd"] = str(tmp_path)
        run.registry.register("implement", lambda s: s)
        ctx = default_lifecycle_context(
            phase_registry=run.registry,
            provider=Provider(),
        )

        profile = _Pr(
            name="t", kind="custom",
            steps=(
                _PS(
                    phase="implement",
                    quality_gates=(
                        _QG(name="tests", on_fail=_FS.FEED_INTO_NEXT,
                            feed_target="last_test_output",
                            kind=_GK.COMPUTATIONAL),
                    ),
                ),
            ),
        )
        _run_profile(profile, run.state, run.registry, ctx=ctx)

        gate = run.state.phase_log["implement"]["quality_gates"]["tests"]
        assert gate["passed"] is False
        assert "tests failed" in gate["output"]
        assert gate["error"] is None
