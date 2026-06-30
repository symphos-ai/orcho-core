"""``PhaseLifecycle.execute_step`` body. Pin each
state-machine transition + ``PhaseStepExecutor`` Protocol contract.

Stages exercised:
 1. before_review ( stub — no-op verified)
 2. execute (LinearPhaseStepExecutor handler dispatch)
 3. halt-check (state.stop() inside handler → HALTED outcome)
 4. gates (step.quality_gates fire after handler)
 5. halt-check after gates (HALT FailStrategy → HALTED)
 6. skip-check (handler stuffs phase_log[name][skipped] → SKIPPED)
 7. after_review ( stub — no-op verified)
 8. adapter (auto-fire SessionAdapter[step.phase])
 9. checkpoint (ctx.on_checkpoint callback fires for COMPLETED)
 10. metrics (ctx.on_metrics callback fires for COMPLETED/HALTED;
 skipped for SKIPPED)

 keeps FSM as parallel path — ``run_profile`` still uses
``_dispatch_one``. switches.
"""
from __future__ import annotations

import pytest

from pipeline.lifecycle import (
    ExecutionModeRegistry,
    LifecycleContext,
    LinearPhaseStepExecutor,
    PhaseLifecycle,
    PhaseStepExecutor,
    StepStatus,
    default_execution_mode_registry,
    default_lifecycle_context,
)
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
    QualityGate,
)


def _state(**kw) -> PipelineState:
    kw.setdefault("plugin", PluginConfig())
    return PipelineState(task="t", project_dir="/p", **kw)


def _ctx(reg: PhaseRegistry, **overrides) -> LifecycleContext:
    """Test-friendly ctx builder using the default factory."""
    return default_lifecycle_context(phase_registry=reg, **overrides)


# ── PhaseStepExecutor Protocol structural typing ─────────────────────────────

class TestPhaseStepExecutorProtocol:
    def test_linear_executor_satisfies_protocol(self) -> None:
        assert isinstance(LinearPhaseStepExecutor(), PhaseStepExecutor)

    def test_custom_stub_satisfies_protocol(self) -> None:
        """Customer plugin / entry_points authors construct
 executors without inheritance."""
        class StubExec:
            def execute(self, step, state, ctx):
                return state
        assert isinstance(StubExec(), PhaseStepExecutor)

    def test_missing_execute_method_rejected(self) -> None:
        class Broken:
            pass  # missing execute
        assert not isinstance(Broken(), PhaseStepExecutor)


# ── ExecutionModeRegistry contract ───────────────────────────────────────────

class TestExecutionModeRegistry:
    def test_register_and_lookup(self) -> None:
        reg = ExecutionModeRegistry()
        exec_ = LinearPhaseStepExecutor()
        reg.register("linear", exec_)
        assert reg.has("linear") is True
        assert reg.get("linear") is exec_

    def test_unknown_get_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown PhaseStep.execution"):
            ExecutionModeRegistry().get("ghost")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            ExecutionModeRegistry().register("", LinearPhaseStepExecutor())

    def test_default_registry_is_linear_only(self) -> None:
        reg = default_execution_mode_registry()
        assert reg.has("linear") is True
        assert isinstance(reg.get("linear"), LinearPhaseStepExecutor)
        # Linear is the only built-in execution mode; plugins register more.
        assert reg.names() == ["linear"]


# ── LinearPhaseStepExecutor: handler dispatch ────────────────────────────────

class TestLinearPhaseStepExecutor:
    def test_calls_handler_from_phase_registry(self) -> None:
        seen: list[str] = []

        def handler(state):
            seen.append("called")
            return state

        reg = PhaseRegistry()
        reg.register("implement", handler)
        ctx = _ctx(reg)
        exec_ = LinearPhaseStepExecutor()
        exec_.execute(PhaseStep(phase="implement"), _state(), ctx)

        assert seen == ["called"]

    def test_handler_returning_none_falls_back_to_input_state(self) -> None:
        reg = PhaseRegistry()
        reg.register("implement", lambda state: None)  # in-place mutation idiom
        ctx = _ctx(reg)
        st = _state()
        result = LinearPhaseStepExecutor().execute(
            PhaseStep(phase="implement"), st, ctx,
        )
        assert result is st

    def test_raises_when_phase_registry_missing(self) -> None:
        ctx = LifecycleContext(phase_registry=None)
        with pytest.raises(ValueError, match="phase_registry is None"):
            LinearPhaseStepExecutor().execute(
                PhaseStep(phase="implement"), _state(), ctx,
            )


# ── PhaseLifecycle.execute_step: COMPLETED happy path ────────────────────────

class TestFSMCompleted:
    def test_completed_outcome_for_normal_handler(self) -> None:
        reg = PhaseRegistry()
        reg.register("implement", lambda s: s)
        ctx = _ctx(reg)
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(phase="implement"), _state(), ctx,
        )
        assert outcome.status is StepStatus.COMPLETED
        assert outcome.reason is None

    def test_completed_invokes_checkpoint_and_metrics_callbacks(self) -> None:
        ckp_calls: list[tuple[str, str]] = []
        metrics_calls: list[tuple[str, str]] = []

        reg = PhaseRegistry()
        reg.register("implement", lambda s: s)
        ctx = _ctx(
            reg,
        )
        ctx.on_checkpoint = lambda name, st: ckp_calls.append((name, "ok"))
        ctx.on_metrics = lambda name, st: metrics_calls.append((name, "ok"))

        PhaseLifecycle().execute_step(PhaseStep(phase="implement"), _state(), ctx)

        assert ckp_calls == [("implement", "ok")]
        assert metrics_calls == [("implement", "ok")]


# ── HALTED transitions ───────────────────────────────────────────────────────

class TestFSMHalted:
    def test_handler_calling_state_stop_yields_halted(self) -> None:
        def stopper(state):
            state.stop("manual halt")
            return state

        reg = PhaseRegistry()
        reg.register("validate_plan", stopper)
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(phase="validate_plan"), _state(), _ctx(reg),
        )
        assert outcome.status is StepStatus.HALTED
        assert outcome.reason == "manual halt"

    def test_halt_fires_adapter_and_metrics_not_checkpoint(self) -> None:
        """HALT fires adapter (writes session shape) + metrics (handler-side
 HALT may follow an expensive agent call), but NOT checkpoint: a halted
 phase is not a completed checkpoint. Recording it would add the phase to
 ``ckpt.completed`` and let resume skip a halted IMPLEMENT, marching the
 run on to review against partial work."""
        adapter_calls: list[tuple[str, int | None]] = []
        ckp = []
        metrics = []

        class _Adapter:
            def write(self, name, state, session, *, round_n=None):
                adapter_calls.append((name, round_n))

        from pipeline.session_adapters import SessionAdapterRegistry
        adapters = SessionAdapterRegistry()
        adapters.register("validate_plan", _Adapter())

        reg = PhaseRegistry()
        reg.register("validate_plan", lambda s: (s.stop("nope") or s))

        session: dict = {"phases": {}}
        ctx = _ctx(
            reg,
            session_adapter_registry=adapters,
            run_config={"session": session},
        )
        ctx.on_checkpoint = lambda *a: ckp.append(a)
        ctx.on_metrics = lambda *a: metrics.append(a)

        PhaseLifecycle().execute_step(
            PhaseStep(phase="validate_plan"), _state(), ctx,
        )
        assert len(ckp) == 0, "halted phase must NOT checkpoint"
        assert adapter_calls == [("validate_plan", None)], (
            "halt should still fire adapter (session shape)"
        )
        assert len(metrics) == 1, "halt should still record consumed usage"


# ── FAILED transitions ───────────────────────────────────────────────────────

class TestFSMFailed:
    def test_handler_raising_yields_failed(self) -> None:
        def boom(state):
            raise RuntimeError("provider crashed")

        reg = PhaseRegistry()
        reg.register("implement", boom)
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(phase="implement"), _state(), _ctx(reg),
        )
        assert outcome.status is StepStatus.FAILED
        assert "RuntimeError" in outcome.reason
        assert "provider crashed" in outcome.reason
        assert isinstance(outcome.error, RuntimeError)
        assert str(outcome.error) == "provider crashed"

    def test_failed_skips_gates_and_callbacks(self) -> None:
        """Handler exception short-circuits — gates / adapter /
 checkpoint / metrics all skipped."""
        gate_calls: list[str] = []
        ckp: list = []
        metrics: list = []

        reg = PhaseRegistry()
        reg.register("implement", lambda s: (_ for _ in ()).throw(RuntimeError("x")))

        gates = QualityGateRegistry()

        class _Stub:
            def execute(self, gate, state, cwd):
                gate_calls.append(gate.name)
                return QualityGateResult(
                    name=gate.name, passed=True, output="",
                    duration_s=0.0, kind=gate.kind,
                )
        gates.register("lint", _Stub())

        ctx = _ctx(reg, quality_gate_registry=gates)
        ctx.on_checkpoint = lambda *a: ckp.append(a)
        ctx.on_metrics = lambda *a: metrics.append(a)

        outcome = PhaseLifecycle().execute_step(
            PhaseStep(
                phase="implement",
                quality_gates=(QualityGate(
                    name="lint", on_fail=FailStrategy.INFORMATIONAL,
                    kind=GateKind.COMPUTATIONAL,
                ),),
            ),
            _state(), ctx,
        )
        assert outcome.status is StepStatus.FAILED
        assert gate_calls == [], "gates skipped on FAILED"
        assert ckp == [], "checkpoint skipped on FAILED"
        assert metrics == [], "metrics skipped on FAILED"


# ── ADR 0009: active_step lifetime ───────────────────────────────────────────

class TestActiveStepLifetime:
    """``LifecycleContext.active_step`` is per-step transient — set on
 entry to ``execute_step``, cleared in ``finally``. Handlers may read
 it within the dispatch, but it must never persist across phases."""

    def test_active_step_visible_to_handler(self) -> None:
        seen: list = []

        def handler(state):
            seen.append(state.lifecycle_ctx.active_step)
            return state

        reg = PhaseRegistry()
        reg.register("review_changes", handler)
        step = PhaseStep(phase="review_changes")
        ctx = _ctx(reg)
        PhaseLifecycle().execute_step(step, _state(), ctx)
        assert seen == [step]

    def test_active_step_cleared_after_completed(self) -> None:
        reg = PhaseRegistry()
        reg.register("review_changes", lambda s: s)
        ctx = _ctx(reg)
        PhaseLifecycle().execute_step(PhaseStep(phase="review_changes"), _state(), ctx)
        assert ctx.active_step is None

    def test_active_step_cleared_after_failed(self) -> None:
        """Even when the handler raises and the FSM returns FAILED, the
 ``finally`` block in ``execute_step`` must clear ``active_step``
 and ``state.lifecycle_ctx``. Otherwise a stale step leaks into
 the next phase."""
        reg = PhaseRegistry()
        reg.register("review_changes", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        ctx = _ctx(reg)
        state = _state()
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(phase="review_changes"), state, ctx,
        )
        assert outcome.status is StepStatus.FAILED
        assert ctx.active_step is None
        assert state.lifecycle_ctx is None


# ── SKIPPED transitions ──────────────────────────────────────────────────────

class TestFSMSkipped:
    def test_handler_marking_skipped_yields_skipped_outcome(self) -> None:
        def review_skip(state):
            state.phase_log["review_changes"] = {"skipped": "no uncommitted"}
            return state

        reg = PhaseRegistry()
        reg.register("review_changes", review_skip)
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(phase="review_changes"), _state(), _ctx(reg),
        )
        assert outcome.status is StepStatus.SKIPPED
        assert outcome.reason == "no uncommitted"

    def test_skipped_skips_checkpoint_and_metrics(self) -> None:
        """Legacy parity: skipped handlers don't record metrics
 (fixup decision)."""
        ckp = []
        metrics = []
        reg = PhaseRegistry()
        reg.register(
            "review_changes",
            lambda s: (s.phase_log.update({"review_changes": {"skipped": "x"}}) or s),
        )
        ctx = _ctx(reg)
        ctx.on_checkpoint = lambda *a: ckp.append(a)
        ctx.on_metrics = lambda *a: metrics.append(a)

        PhaseLifecycle().execute_step(
            PhaseStep(phase="review_changes"), _state(), ctx,
        )
        assert ckp == [], "skipped phase doesn't checkpoint"
        assert metrics == [], "skipped phase doesn't record metrics"

    def test_skipped_does_not_fire_gates(self) -> None:
        gate_calls: list[str] = []

        reg = PhaseRegistry()
        reg.register(
            "review_changes",
            lambda s: (s.phase_log.update({"review_changes": {"skipped": "x"}}) or s),
        )
        gates = QualityGateRegistry()

        class _Stub:
            def execute(self, gate, state, cwd):
                gate_calls.append(gate.name)
                return QualityGateResult(
                    name=gate.name, passed=True, output="",
                    duration_s=0.0, kind=gate.kind,
                )
        gates.register("lint", _Stub())

        ctx = _ctx(reg, quality_gate_registry=gates)
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(
                phase="review_changes",
                quality_gates=(QualityGate(
                    name="lint", on_fail=FailStrategy.INFORMATIONAL,
                    kind=GateKind.COMPUTATIONAL,
                ),),
            ),
            _state(), ctx,
        )
        assert outcome.status is StepStatus.SKIPPED
        assert gate_calls == [], "skipped handler → gates don't fire"


# ── Quality gates inside FSM ─────────────────────────────────────────────────

class TestFSMGates:
    def test_gates_fire_after_execute(self) -> None:
        sequence: list[str] = []

        reg = PhaseRegistry()
        reg.register("implement", lambda s: (sequence.append("handler") or s))

        gates = QualityGateRegistry()

        class _Stub:
            def execute(self, gate, state, cwd):
                sequence.append(f"gate:{gate.name}")
                return QualityGateResult(
                    name=gate.name, passed=True, output="",
                    duration_s=0.0, kind=gate.kind,
                )
        gates.register("lint", _Stub())

        ctx = _ctx(reg, quality_gate_registry=gates)
        PhaseLifecycle().execute_step(
            PhaseStep(
                phase="implement",
                quality_gates=(QualityGate(
                    name="lint", on_fail=FailStrategy.INFORMATIONAL,
                    kind=GateKind.COMPUTATIONAL,
                ),),
            ),
            _state(), ctx,
        )

        # Handler runs first; then gate.
        assert sequence == ["handler", "gate:lint"]

    def test_halt_gate_yields_halted_outcome(self) -> None:
        """HALT FailStrategy fires after gate fails → state.halt → FSM
 returns HALTED."""
        reg = PhaseRegistry()
        reg.register("implement", lambda s: s)

        gates = QualityGateRegistry()

        class _FailStub:
            def execute(self, gate, state, cwd):
                return QualityGateResult(
                    name=gate.name, passed=False, output="security leak",
                    duration_s=0.0, kind=gate.kind,
                )
        gates.register("security", _FailStub())

        ctx = _ctx(reg, quality_gate_registry=gates)
        outcome = PhaseLifecycle().execute_step(
            PhaseStep(
                phase="implement",
                quality_gates=(QualityGate(
                    name="security", on_fail=FailStrategy.HALT,
                    kind=GateKind.INFERENTIAL,
                ),),
            ),
            _state(), ctx,
        )
        assert outcome.status is StepStatus.HALTED
        # Reason mentions either the explicit halt or generic halt-after-gate.
        assert outcome.reason

    def test_halt_gate_fires_adapter_and_metrics_not_checkpoint(self) -> None:
        """Regression: gate-side HALT has the same persistence contract as
 handler-side HALT. The failing gate record must reach session shape
 (adapter) and record consumed usage (metrics), but a gate-halted phase
 is NOT a completed checkpoint, so ``on_checkpoint`` must not fire."""
        adapter_calls: list[tuple[str, int | None]] = []
        ckp: list[tuple[str, PipelineState]] = []
        metrics: list = []

        class _Adapter:
            def write(self, name, state, session, *, round_n=None):
                adapter_calls.append((name, round_n))
                session.setdefault("phases", {})[name] = dict(state.phase_log[name])

        from pipeline.session_adapters import SessionAdapterRegistry

        adapters = SessionAdapterRegistry()
        adapters.register("implement", _Adapter())

        reg = PhaseRegistry()
        reg.register(
            "implement",
            lambda s: (s.phase_log.update({"implement": {"output": "built"}}) or s),
        )

        gates = QualityGateRegistry()

        class _FailStub:
            def execute(self, gate, state, cwd):
                return QualityGateResult(
                    name=gate.name, passed=False, output="security leak",
                    duration_s=0.0, kind=gate.kind,
                )
        gates.register("security", _FailStub())

        session = {"phases": {}}
        ctx = _ctx(
            reg,
            quality_gate_registry=gates,
            session_adapter_registry=adapters,
            run_config={"session": session},
        )
        ctx.on_checkpoint = lambda name, st: ckp.append((name, st))
        ctx.on_metrics = lambda *a: metrics.append(a)

        outcome = PhaseLifecycle().execute_step(
            PhaseStep(
                phase="implement",
                quality_gates=(QualityGate(
                    name="security", on_fail=FailStrategy.HALT,
                    kind=GateKind.INFERENTIAL,
                ),),
            ),
            _state(), ctx,
        )

        assert outcome.status is StepStatus.HALTED
        assert adapter_calls == [("implement", None)]
        assert len(ckp) == 0, "gate-halted phase must NOT checkpoint"
        assert session["phases"]["implement"]["quality_gates"]["security"]["passed"] is False
        assert len(metrics) == 1


# ── Adapter auto-fire ────────────────────────────────────────────────────────

class TestFSMAdapter:
    def test_adapter_fires_for_completed(self) -> None:
        adapter_calls: list[tuple[str, dict]] = []

        class _Adapter:
            def write(self, name, state, session, *, round_n=None):
                adapter_calls.append((name, dict(session)))

        from pipeline.session_adapters import SessionAdapterRegistry
        adapters = SessionAdapterRegistry()
        adapters.register("implement", _Adapter())

        reg = PhaseRegistry()
        reg.register("implement", lambda s: (s.phase_log.update({"implement": {"out": "ok"}}) or s))

        session: dict = {"phases": {}}
        ctx = _ctx(
            reg,
            session_adapter_registry=adapters,
            run_config={"session": session},
        )
        PhaseLifecycle().execute_step(
            PhaseStep(phase="implement"), _state(), ctx,
        )
        # Adapter received the call.
        assert len(adapter_calls) == 1
        assert adapter_calls[0][0] == "implement"


# ── wiring: run_profile dispatches PhaseStep via FSM when ctx ─────

class TestRunProfileFSMDispatchWithContext:
    """when ``ctx`` is passed to ``run_profile``,
 PhaseStep entries dispatch via ``PhaseLifecycle.execute_step``
 (FSM); when ``ctx`` is None, legacy ``_dispatch_one`` path is
 used (test backward-compat). Snapshot fixtures pass through both
 paths because identical session shape is preserved."""

    def test_phasestep_entry_dispatches_via_fsm_when_ctx_provided(
        self,
    ) -> None:
        """Verify FSM was invoked by checking handler ran AND
 ctx-routed gate fired."""
        sequence: list[str] = []
        reg = PhaseRegistry()
        reg.register("implement", lambda s: (sequence.append("handler") or s))

        gates = QualityGateRegistry()

        class _Stub:
            def execute(self, gate, state, cwd):
                sequence.append(f"gate:{gate.name}")
                return QualityGateResult(
                    name=gate.name, passed=True, output="",
                    duration_s=0.0, kind=gate.kind,
                )
        gates.register("lint", _Stub())

        ctx = _ctx(reg, quality_gate_registry=gates)
        # Dispatch via run_profile WITH ctx — FSM path active.
        from pipeline.runtime import Profile, run_profile

        profile = Profile(
            name="x", kind="custom",
            steps=(PhaseStep(
                phase="implement",
                quality_gates=(QualityGate(
                    name="lint", on_fail=FailStrategy.INFORMATIONAL,
                    kind=GateKind.COMPUTATIONAL,
                ),),
            ),),
        )
        run_profile(profile, _state(), reg, ctx=ctx)

        # FSM dispatched: handler ran, then gate fired (FSM stage 4).
        assert sequence == ["handler", "gate:lint"]

    def test_phasestep_entry_dispatches_via_legacy_when_ctx_none(
        self,
    ) -> None:
        """Backward-compat: tests that don't construct ``ctx`` still
 get dispatch via legacy ``_dispatch_one``. Quality gates fire
 through the same ``_fire_step_quality_gates`` helper, so behavior
 is observable as identical."""
        sequence: list[str] = []
        reg = PhaseRegistry()
        reg.register("implement", lambda s: (sequence.append("handler") or s))

        from pipeline.runtime import Profile, run_profile

        profile = Profile(
            name="x", kind="custom",
            steps=(PhaseStep(phase="implement"),),
        )
        # No ctx → legacy _dispatch_one path.
        run_profile(profile, _state(), reg)

        assert sequence == ["handler"]

    def test_legacy_dispatch_one_still_callable(self) -> None:
        """keeps ``_dispatch_one`` for legacy str entries
 and tests that don't construct ``ctx``. Substeps 4-6 may
 delete it once handlers fully migrate."""
        from pipeline.runtime import _dispatch_one
        assert callable(_dispatch_one)
