"""pipeline/quality_gates.py — First-class verification + fail policy (Phase 4).

A *quality gate* is a registered post-phase check that produces a typed
``QualityGateResult`` and applies a declared fail policy
(``HALT`` / ``FEED_INTO_NEXT`` / ``TRIGGER_REPLAN`` / ``INFORMATIONAL``)
to ``PipelineState`` mutation.

Phase 4 ships:
  * ``QualityGateResult`` — frozen dataclass; one row per gate invocation
  * ``QualityGateHandler`` Protocol — plugin contract
  * ``QualityGateRegistry`` — lookup by gate name
  * ``TestsGate`` (built-in) — wraps the legacy ``run_tests`` shell logic
  * ``run_quality_gate`` / ``apply_fail_strategy`` — dispatcher helpers

Phase 5e made ``PhaseStep.quality_gates`` active: gates fire inside the
runtime / lifecycle dispatch path after the phase handler and before
adapter/checkpoint/metrics. ``PluginConfig.quality_gates["tests"]`` is the
customer-facing config source for the built-in ``tests`` gate. The
``TestingConfig`` / ``TestSuiteConfig`` dataclasses remain only as
internal coercion targets for the existing ``run_tests`` implementation.

See ``docs/architecture/quality_gates.md`` and ``docs/adr/0007``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pipeline.runtime import (
    FailStrategy,
    GateKind,
    PipelineState,
    QualityGate,
)

# ── QualityGateResult ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class QualityGateResult:
    """Outcome of one gate invocation. Frozen — record-shaped, suitable
    for serialization into ``state.phase_log[name]["quality_gates"][gate]``
    and the events.jsonl audit trail.
    """
    name: str
    passed: bool
    output: str
    duration_s: float
    kind: GateKind = GateKind.COMPUTATIONAL
    cost_usd: float | None = None  # for inferential gates only
    error: str | None = None       # populated when handler raised

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("QualityGateResult.name is empty")


# ── QualityGateHandler Protocol ──────────────────────────────────────────────

@runtime_checkable
class QualityGateHandler(Protocol):
    """Plugin contract. The gate's ``execute`` reads from the project
    + plugin config and returns a ``QualityGateResult``.

    Handlers should NEVER raise — exceptions caught at the dispatcher
    level become ``QualityGateResult(passed=False, error=...)`` so a
    broken test runner doesn't crash the pipeline. The result then
    drives the fail policy.
    """

    def execute(
        self,
        gate: QualityGate,
        state: PipelineState,
        cwd: str,
    ) -> QualityGateResult: ...


# ── QualityGateRegistry ──────────────────────────────────────────────────────

class QualityGateRegistry:
    """Map gate name → handler. Plugin extension via the future
    ``orcho.quality_gates`` entry_points group (Phase 7).

    Lookup priority chain (resolved by callers, not the registry):
      1. ``QualityGate.config`` (per-step from profile JSON)
      2. ``plugin.quality_gates[name]`` defaults
      3. handler internals
    """

    def __init__(self) -> None:
        self._handlers: dict[str, QualityGateHandler] = {}

    def register(self, name: str, handler: QualityGateHandler) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("quality gate name must be a non-empty string")
        self._handlers[name.strip()] = handler

    def get(self, name: str) -> QualityGateHandler:
        if name not in self._handlers:
            raise KeyError(
                f"Unknown quality gate {name!r}. "
                f"Registered: {sorted(self._handlers)}"
            )
        return self._handlers[name]

    def get_or_none(self, name: str) -> QualityGateHandler | None:
        return self._handlers.get(name)

    def has(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._handlers)


# ── TestsGate (built-in, computational) ──────────────────────────────────────

class TestsGate:
    """Computational gate: runs the project's test suite(s) per
    ``plugin.quality_gates["tests"]``. Phase 4 ports the body of the legacy
    ``run_tests`` from ``pipeline.project_orchestrator`` — no semantic
    changes.

    Reads:
      * ``state.lifecycle_ctx.provider`` — orchestrator-owned provider
        handle carried by the FSM context. Its ``run_tests(cwd, plugin)``
        override wins. Mock providers use this to short-circuit the
        subprocess for deterministic tests. ``None``-return signals
        "no override — fall through to the subprocess path".
      * ``state.plugin.quality_gates["tests"]`` — dict config coerced by
        ``project_orchestrator._resolve_tests_config`` into an internal
        TestingConfig for the subprocess fallback.
      * ``cwd`` — passed by caller (orchestrator's ``git_cwd``).

    Returns ``QualityGateResult(passed=True/False, output=combined_stdout)``.
    Skipped suites (no run_command) yield ``passed=True, output=""`` to
    match legacy ``TestResult(skipped=True)`` semantics.
    """

    def execute(
        self,
        gate: QualityGate,
        state: PipelineState,
        cwd: str,
    ) -> QualityGateResult:
        import time

        from core.observability import events as _events
        t0 = time.monotonic()
        _events.emit(
            "gate.start", name=gate.name, gate_kind=GateKind.COMPUTATIONAL.value,
        )
        # Phase 5e-5 pre-Phase-6 hardening: provider-override path now
        # reads from the typed LifecycleContext instead of a production
        # ``state.extras["_provider"]`` channel. The extras fallback is
        # kept only for isolated direct-unit tests and custom callers that
        # invoke TestsGate outside the FSM.
        ctx = getattr(state, "lifecycle_ctx", None)
        provider = getattr(ctx, "provider", None) if ctx is not None else None
        if provider is None:
            provider = state.extras.get("_provider")
        tr = None
        if provider is not None:
            try:
                tr = provider.run_tests(cwd, state.plugin)
            except Exception as e:
                duration = time.monotonic() - t0
                _events.emit(
                    "gate.end", name=gate.name, outcome="failed",
                    duration_s=round(duration, 3),
                    error=f"{type(e).__name__}: {e}",
                )
                return QualityGateResult(
                    name=gate.name,
                    passed=False,
                    output=f"provider.run_tests raised: {type(e).__name__}: {e}",
                    duration_s=duration,
                    kind=GateKind.COMPUTATIONAL,
                    error=str(e),
                )
        if tr is None:
            # Either no provider, or provider returned None (signal:
            # "use the subprocess path"). Fall through to the canonical
            # ``run_tests`` impl in ``pipeline.project_testing`` (the
            # legacy ``pipeline.project_orchestrator.run_tests`` shim
            # retired in ADR 0042 Phase J).
            from pipeline.project_testing import run_tests as _run_tests_impl
            try:
                tr = _run_tests_impl(cwd, state.plugin)
            except Exception as e:
                duration = time.monotonic() - t0
                _events.emit(
                    "gate.end", name=gate.name, outcome="failed",
                    duration_s=round(duration, 3),
                    error=f"{type(e).__name__}: {e}",
                )
                return QualityGateResult(
                    name=gate.name,
                    passed=False,
                    output=f"TestsGate execute raised: {type(e).__name__}: {e}",
                    duration_s=duration,
                    kind=GateKind.COMPUTATIONAL,
                    error=str(e),
                )
        duration = time.monotonic() - t0
        # Legacy TestResult.skipped=True is equivalent to "no failures
        # to surface" — gate result is passed=True with empty output.
        if tr.skipped:
            _events.emit(
                "gate.end", name=gate.name, outcome="skipped",
                duration_s=round(duration, 3),
            )
            return QualityGateResult(
                name=gate.name,
                passed=True,
                output="",
                duration_s=duration,
                kind=GateKind.COMPUTATIONAL,
            )
        outcome = "passed" if tr.passed else "failed"
        _events.emit(
            "gate.end", name=gate.name, outcome=outcome,
            duration_s=round(tr.duration if tr.duration else duration, 3),
        )
        return QualityGateResult(
            name=gate.name,
            passed=bool(tr.passed),
            output=tr.output or "",
            duration_s=tr.duration if tr.duration else duration,
            kind=GateKind.COMPUTATIONAL,
        )


# ── Dispatcher helpers ───────────────────────────────────────────────────────

def run_quality_gate(
    gate: QualityGate,
    state: PipelineState,
    cwd: str,
    registry: QualityGateRegistry,
) -> QualityGateResult:
    """Resolve handler from registry, invoke, and capture the result.

    Handler exceptions are caught and surfaced in ``result.error`` —
    a broken test runner doesn't crash the pipeline. Unknown gate names
    raise ``KeyError`` (loud — that's a profile bug, not a runtime
    exception).
    """
    handler = registry.get(gate.name)
    import time
    t0 = time.monotonic()
    try:
        return handler.execute(gate, state, cwd)
    except Exception as e:
        duration = time.monotonic() - t0
        return QualityGateResult(
            name=gate.name,
            passed=False,
            output=f"QualityGate {gate.name!r} handler raised: {type(e).__name__}: {e}",
            duration_s=duration,
            kind=gate.kind,
            error=str(e),
        )


def apply_fail_strategy(
    gate: QualityGate,
    result: QualityGateResult,
    state: PipelineState,
) -> bool:
    """Apply the gate's fail policy when the result is failing.

    Returns True when the run should halt (caller breaks the
    profile / loop), False otherwise. Side effects on ``state``
    depend on ``gate.on_fail``:

      * HALT — ``state.stop(...)`` fired; caller halts.
      * FEED_INTO_NEXT — ``state.extras[gate.feed_target] = result.output``;
        the canonical ``last_test_output`` target also syncs the typed
        ``state.last_test_output`` field.
      * TRIGGER_REPLAN — ``state.last_critique = result.output`` so the
        next loop round picks up the critique.
      * INFORMATIONAL — log only; no state mutation.

    Passing results (``result.passed == True``) are no-ops regardless of
    strategy.
    """
    if result.passed:
        return False

    match gate.on_fail:
        case FailStrategy.HALT:
            state.stop(f"quality gate {gate.name!r} failed (on_fail=HALT)")
            return True
        case FailStrategy.FEED_INTO_NEXT:
            target = gate.feed_target or "last_test_output"
            state.extras[target] = result.output
            if target == "last_test_output":
                state.last_test_output = result.output
            return False
        case FailStrategy.TRIGGER_REPLAN:
            state.last_critique = result.output
            return False
        case FailStrategy.INFORMATIONAL:
            return False
    # Unreachable — every FailStrategy enum value covered above.
    return False  # pragma: no cover


# ── Default registry singleton ───────────────────────────────────────────────

_DEFAULT_REGISTRY: QualityGateRegistry | None = None


def default_quality_gate_registry() -> QualityGateRegistry:
    """Singleton built-in registry. Phase 4 shipped ``tests`` only;
    Phase 7c discovers customer gates via the ``orcho.quality_gates``
    entry_points group (lint, compile, security_review,
    spec_compliance, etc.).

    Plugin gates with names matching built-ins replace them. Entry
    contract: each entry_points value resolves to either a
    ``QualityGateHandler`` instance directly, or a zero-arg callable
    returning one (e.g. a class with no-arg ``__init__``). See
    ``pipeline.entry_points`` for the discovery semantics.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        reg = QualityGateRegistry()
        reg.register("tests", TestsGate())
        # Phase 7c: customer plugins ship additional gates via
        # ``orcho.quality_gates`` entry_points. Load failures are
        # logged per-entry; one bad plugin doesn't block discovery.
        from pipeline.entry_points import register_entry_points
        register_entry_points(reg, "orcho.quality_gates")
        _DEFAULT_REGISTRY = reg
    return _DEFAULT_REGISTRY
