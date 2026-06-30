"""pipeline.lifecycle — Phase lifecycle FSM types (Phase 5e step 5
expands).

Phase 1 introduced the types; Phase 1.5 documented the FSM contract.
Phase 5e step 1 (this commit) adds the typed Protocol fields the FSM
needs to break the ``builtin_phases ↔ project_orchestrator`` circular
tension that produced 9 lazy imports inside handler bodies. Substep 2+
wire the FSM as the active dispatch engine.

Phase 5e step 5 substep 1 deliverable: ``LifecycleContext`` carries
the typed Protocol fields handlers will read in substep 4 to replace
the lazy imports. NB: this commit is **types + factory only** — no
call sites use ``LifecycleContext`` yet.

Replaces ad-hoc ``state.halt: bool`` channel with typed ``StepOutcome``
that carries reason + retry payload. See
``docs/architecture/phase_lifecycle.md`` (Phase 1.5) and
``.orcho/artifacts/phase5e5_step_outcome_design.md`` (Phase 5e step 5
design pass).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid circular import at module load
    from agents.protocols import SessionMode
    from agents.runtimes import AgentProvider
    from pipeline.plan_parser import ParsedPlan
    from pipeline.plugins import PluginConfig, TestingConfig
    from pipeline.runtime import PhaseStep, PipelineState


class StepStatus(StrEnum):
    """Outcome category for one PhaseStep through the lifecycle FSM."""
    COMPLETED = "completed"              # normal pass-through
    SKIPPED = "skipped"                  # SKIP action / unmet condition
    RETRY_REQUESTED = "retry_requested"  # RETRY/REPROMPT — caller re-executes
    HALTED = "halted"                    # HALT action / state.stop()
    FAILED = "failed"                    # exception, captured


@dataclass(frozen=True)
class StepOutcome:
    """Typed result of one PhaseStep execution. Replaces
    ``state.halt: bool`` mutation as the only control channel.

    For ``status=RETRY_REQUESTED``, ``retry_payload`` carries:
      * ``loop_round_delta``: int — usually +1 (advance to next round)
      * ``critique``: str | None — text fed into next round's handler
      * ``trigger``: "human" | "agent_ask" — origin of retry signal

    For terminal statuses (HALTED / FAILED / SKIPPED), ``reason`` is
    a human-readable explanation surfacing in run summary + meta.json.
    """
    status: StepStatus
    state: Any                            # PipelineState — typed loosely to
                                          # avoid circular import (Phase 1.5
                                          # design will resolve module layout)
    reason: str | None = None
    retry_payload: dict | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        if self.status is StepStatus.RETRY_REQUESTED and not self.retry_payload:
            raise ValueError(
                "StepOutcome(status=RETRY_REQUESTED) requires retry_payload"
            )
        if self.status in (StepStatus.HALTED, StepStatus.FAILED, StepStatus.SKIPPED) \
                and not self.reason:
            raise ValueError(
                f"StepOutcome(status={self.status.value}) requires reason"
            )


# ── Helper Protocols (Phase 5e step 5 substep 1) ─────────────────────────────
#
# Each Protocol bundles what handlers currently lazy-import from
# ``pipeline.project_orchestrator``. ``LifecycleContext`` carries one
# field per Protocol; handlers receive the context (substep 4) and
# read methods through it. Tests construct stubs directly.


@runtime_checkable
class PlanHelpers(Protocol):
    """Plan path validation helper for canonical ParsedPlan objects."""

    def validate_paths(
        self,
        plan: ParsedPlan,
        project_dir: str,
    ) -> tuple[list[str], list[str]]:
        """Return (existing_files, missing_files) lists from
        ``plan.file_paths`` against ``project_dir``."""
        ...


@runtime_checkable
class GitHelpers(Protocol):
    """Git working-tree queries. Replaces lazy import of
    ``has_uncommitted`` from ``core.io.git_helpers`` inside
    ``_phase_review`` (which goes via ``pipeline.project_orchestrator``
    re-export so existing test mocks take effect — Phase 5d-fixup
    pattern).
    """

    def has_uncommitted(self, cwd: str) -> bool:
        """True if working tree at ``cwd`` has uncommitted changes."""
        ...


@runtime_checkable
class TextHelpers(Protocol):
    """Text-classification helpers for typed review contract state."""

    def critique_is_empty(self, text: str) -> bool:
        """True if ``text`` is empty or an APPROVED JSON review."""
        ...


# Type aliases for the function-shaped helpers (no Protocol needed —
# pure callables).

SessionModeResolver = Callable[..., "SessionMode"]
"""Resolve session mode (CHAIN / HYBRID / STATELESS) from per-round
config. Replaces lazy import of ``_resolve_session_mode`` inside
``_resolve_fix_runtime_config``."""

TestConfigResolver = Callable[["PluginConfig"], "TestingConfig"]
"""Resolve TestingConfig from PluginConfig.quality_gates dict.
Replaces lazy import of ``_resolve_tests_config`` inside ``_phase_fix``
write_style read."""


# ── LifecycleContext ──────────────────────────────────────────────────────────


@dataclass
class LifecycleContext:
    """Per-step execution context threaded through the lifecycle FSM.

    Carries injected registries (phase handlers, session adapters, gate
    runners, human review backend), Protocol-typed helpers (plan / git
    / text), and callbacks (event emit, metrics record, checkpoint
    save).

    Phase 5e step 5 substep 1 (this dataclass) ships the typed
    contract. Substep 2 wires ``PhaseLifecycle.execute_step`` body.
    Substep 3 switches ``run_profile`` to dispatch through it. Substep
    4 migrates handlers to read fields off the context.

    Field ownership map:
      * ``phase_registry`` — Phase 1 type; populated by orchestrator
      * ``session_adapter_registry`` / ``quality_gate_registry`` —
        Phase 3 / Phase 4 types; populated by orchestrator
      * ``human_review_backend`` — Phase 8 type; populated by
        orchestrator (TBD in Phase 8 implementation)
      * ``provider`` — typed replacement for the former production
        ``state.extras["_provider"]`` channel; TestsGate reads this
        for mock/custom provider overrides
      * ``run_config`` — bundle of run-level settings (session_mode,
        models, codemap, etc.); replaces 6+ ad-hoc state.extras keys
      * ``plan_helpers`` / ``git_helpers`` / ``text_helpers`` — Protocol
        stubs that bundle the 5 lazy imports in ``builtin_phases.py``
      * ``session_mode_resolver`` / ``test_config_resolver`` — pure
        callables that bundle the 2 remaining lazy imports
      * ``on_event`` / ``on_metrics`` / ``on_checkpoint`` — event /
        metrics / checkpoint callbacks (existed in Phase 1.5 design;
        substep 3 wires real impls from ``_PipelineRun``)
    """
    phase_registry: Any                           # PhaseRegistry
    session_adapter_registry: Any = None          # SessionAdapterRegistry
    quality_gate_registry: Any = None             # QualityGateRegistry
    execution_mode_registry: Any = None           # ExecutionModeRegistry (substep 2)
    human_review_backend: Any = None              # HumanReviewBackend (Phase 8)

    # Phase 5e step 5 substep 1 NEW fields:
    provider: AgentProvider | None = None
    run_config: dict[str, Any] = field(default_factory=dict)
    plan_helpers: PlanHelpers | None = None
    git_helpers: GitHelpers | None = None
    text_helpers: TextHelpers | None = None
    session_mode_resolver: SessionModeResolver | None = None
    test_config_resolver: TestConfigResolver | None = None

    # Phase 1.5 callbacks (orchestrator wires real impls in substep 3):
    on_event: Callable[[str, dict], None] | None = None
    on_metrics: Callable[[str, Any], None] | None = None  # (name, state) → None
    on_checkpoint: Callable[[str, Any], None] | None = None  # (name, state) → None

    # Per-step transient: set by ``PhaseLifecycle.execute_step`` before
    # dispatch, cleared in ``finally``. Handlers read it within the single
    # dispatch call only — never across phases. Stale reads after a phase
    # ends are a bug.
    active_step: PhaseStep | None = None


def default_lifecycle_context(
    phase_registry: Any,
    *,
    quality_gate_registry: Any = None,
    session_adapter_registry: Any = None,
    execution_mode_registry: Any = None,
    provider: AgentProvider | None = None,
    run_config: dict[str, Any] | None = None,
) -> LifecycleContext:
    """Phase 5e step 5 substep 1 factory: build a ``LifecycleContext``
    populated with default Protocol implementations from the existing
    module-level helpers in ``pipeline.project_orchestrator``.

    This factory is the **bridge** between today's lazy-import
    architecture and the substep-4 future where handlers read off the
    context directly. Plugin authors can construct their own
    ``LifecycleContext`` to override individual helpers (e.g. mock
    ``git_helpers`` for a CI-only profile that skips no-uncommitted
    detection).

    Lazy imports in the factory body are intentional — defer the
    ``project_orchestrator`` import until factory call time so this
    module itself stays importable without circular issues.
    """
    if run_config is None:
        run_config = {}

    # Default helper implementations bind to the canonical home of each
    # function (ADR 0042 Phase J: the legacy ``_orch.X`` indirection
    # retired with the shim's alias block). The dataclass fields type
    # these as Protocol but Python's structural typing accepts any
    # object exposing the named methods.
    from core.io.git_helpers import has_uncommitted as _has_uncommitted
    from pipeline.project.handoff import critique_is_empty as _critique_is_empty
    from pipeline.project.runtime_setup import (
        _resolve_session_mode,
        _validate_plan_file_paths,
    )
    from pipeline.project_testing import resolve_tests_config as _resolve_tests_config

    class _DefaultPlanHelpers:
        def validate_paths(self, plan, project_dir: str):
            return _validate_plan_file_paths(plan, project_dir)

    class _DefaultGitHelpers:
        def has_uncommitted(self, cwd: str) -> bool:
            return _has_uncommitted(cwd)

    class _DefaultTextHelpers:
        def critique_is_empty(self, text: str) -> bool:
            return _critique_is_empty(text)

    if execution_mode_registry is None:
        execution_mode_registry = default_execution_mode_registry()

    return LifecycleContext(
        phase_registry=phase_registry,
        quality_gate_registry=quality_gate_registry,
        session_adapter_registry=session_adapter_registry,
        execution_mode_registry=execution_mode_registry,
        provider=provider,
        run_config=run_config,
        plan_helpers=_DefaultPlanHelpers(),
        git_helpers=_DefaultGitHelpers(),
        text_helpers=_DefaultTextHelpers(),
        session_mode_resolver=_resolve_session_mode,
        test_config_resolver=_resolve_tests_config,
    )


# ── Phase-step execution mode (Phase 5e-5 substep 2) ─────────────────────────
#
# ``PhaseStepExecutor`` is the per-PhaseStep dispatch strategy. The FSM
# body (``PhaseLifecycle.execute_step``) resolves the right executor
# from ``ctx.execution_mode_registry`` based on ``step.execution`` and
# delegates handler invocation. ``LinearPhaseStepExecutor`` is the default
# (calls the handler directly). Built-in subtask delivery is policy-owned by
# ``_phase_implement`` (``implementation_execution=subtask_dag``), not a
# profile-step execution mode. Additional execution modes are plugin-registered
# via the ``orcho.execution_modes`` entry-point group.


@runtime_checkable
class PhaseStepExecutor(Protocol):
    """Per-PhaseStep dispatch strategy. ``execute`` is invoked by
    ``PhaseLifecycle.execute_step`` after ``before_review`` and BEFORE
    ``gates``. The executor is responsible for invoking the underlying
    phase handler(s) and returning a (possibly mutated) ``PipelineState``.

    Implementations:
      * ``LinearPhaseStepExecutor`` (substep 2) — single handler call
        from ``ctx.phase_registry`` keyed by ``step.phase``
    """

    def execute(
        self,
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> PipelineState: ...


class LinearPhaseStepExecutor:
    """Phase 5e-5 substep 2 default executor. Calls the handler
    registered in ``ctx.phase_registry`` for ``step.phase``. Mutates
    ``state.phase_log[step.phase]`` per the handler's contract.

    Quality gates fire OUTSIDE this executor (``PhaseLifecycle.execute_step``
    invokes ``_fire_step_quality_gates`` between this and ``after_review``).
    Keeps executor concerned only with handler dispatch — gate semantics
    are FSM-stage concern.
    """

    def execute(
        self,
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> PipelineState:
        if ctx.phase_registry is None:
            raise ValueError(
                "LinearPhaseStepExecutor: ctx.phase_registry is None "
                f"(step.phase={step.phase!r})"
            )
        handler = ctx.phase_registry.get(step.phase)
        result = handler(state)
        # Phase 5e-5 substep 6: defensive isinstance check mirrors the
        # pre-substep-2 ``_dispatch_one`` behaviour. Public ``IPhaseHandler``
        # Protocol is ``(state) -> PipelineState | None``; some legacy
        # in-process handlers return ``state.phase_log.setdefault(...)
        # or state`` which can yield the inner dict when ``setdefault``'s
        # default value is truthy. Without the isinstance guard the FSM
        # body crashes at the next ``state.halt`` access. Keep handlers
        # honest: only adopt the return value when it really is a state.
        from pipeline.runtime import PipelineState as _PS
        if isinstance(result, _PS):
            return result
        return state


class ExecutionModeRegistry:
    """Phase 5e-5 substep 2 registry: maps ``step.execution`` string →
    ``PhaseStepExecutor`` instance. Phase 7 plugins extend via
    ``orcho.execution_modes`` entry_points.

    Distinct from the legacy ``pipeline.execution_modes.ExecutionModeRegistry``
    (which keys by entry-name for the v1-shape composite). Substep 5
    deletes the legacy and renames this class to take its place.
    """

    def __init__(self) -> None:
        self._modes: dict[str, PhaseStepExecutor] = {}

    def register(self, name: str, executor: PhaseStepExecutor) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("execution mode name must be a non-empty string")
        self._modes[name.strip()] = executor

    def get(self, name: str) -> PhaseStepExecutor:
        if name not in self._modes:
            raise KeyError(
                f"Unknown PhaseStep.execution {name!r}; "
                f"registered: {sorted(self._modes)}"
            )
        return self._modes[name]

    def has(self, name: str) -> bool:
        return name in self._modes

    def names(self) -> list[str]:
        return sorted(self._modes)


def default_execution_mode_registry() -> ExecutionModeRegistry:
    """Phase 5e-5 substep 2 factory: ``linear`` is wired by default.

    Implement subtask delivery is selected by
    ``pipeline.implementation_execution=subtask_dag`` and handled inside the
    implement phase — it is not a profile-step execution mode, so only
    ``linear`` (plus plugin-registered modes) is dispatchable here.

    Phase 7c: built-ins are registered first, then plugin-shipped
    executors are discovered via the ``orcho.execution_modes``
    importlib.metadata entry_points group. Plugin entries with names
    matching built-ins (``"linear"``) win — that's the
    supported plugin-override mechanism (e.g. shipping a richer
    executor under a plugin-owned name).
    """
    reg = ExecutionModeRegistry()
    reg.register("linear", LinearPhaseStepExecutor())

    # Phase 7c: load plugin-shipped executors. Failures inside
    # ``ep.load()`` are caught per-entry; one bad plugin doesn't block
    # discovery for the rest.
    from pipeline.entry_points import register_entry_points
    register_entry_points(reg, "orcho.execution_modes")

    return reg


# ── PhaseLifecycle FSM body (Phase 5e-5 substep 2 — active engine) ───────────


class PhaseLifecycle:
    """Orchestrates one PhaseStep through the FSM transitions:

        before_review → execute → gates → after_review → adapter
                                                     → checkpoint → metrics

    Phase 5e-5 substep 2 implementation. Each stage may short-circuit
    via ``StepOutcome`` (HALTED if state.halt; FAILED if execute raises;
    SKIPPED if handler set ``phase_log[name][skipped]``). RETRY_REQUESTED
    is reserved for HumanReview (Phase 8) — substep 2 doesn't produce it.

    Substep 2 keeps the FSM as a parallel path: ``run_profile`` still
    uses ``_dispatch_one``. Substep 3 switches.

    See docs/architecture/phase_lifecycle.md for the full transition
    matrix and ordering rules.
    """

    def execute_step(
        self,
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> StepOutcome:
        """Public entry — wraps the FSM body in try/finally to guarantee
        ``state.lifecycle_ctx`` is cleared regardless of outcome.

        Phase 5e-5 substep 6c: ctx now lives on a typed ``PipelineState``
        field (``lifecycle_ctx``) instead of the substep-4 ad-hoc
        ``state.extras["_lifecycle_ctx"]`` string-keyed channel.
        Lifetime-managed by this method: set on entry, cleared on exit.
        Handlers read off it via
        ``pipeline.phases.builtin._ensure_lifecycle_ctx``.
        """
        state.lifecycle_ctx = ctx
        ctx.active_step = step
        try:
            return self._execute_step_body(step, state, ctx)
        finally:
            ctx.active_step = None
            state.lifecycle_ctx = None

    def _execute_step_body(
        self,
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> StepOutcome:
        """FSM body. Returns typed ``StepOutcome``. Stages:

          1. before_review (Phase 8 HumanReview before-hook — currently no-op)
          2. execute (resolve PhaseStepExecutor by step.execution → run)
          3. halt-check (if state.stop() called inside execute → HALTED)
          4. gates (fire step.quality_gates, accumulate to phase_log)
          5. halt-check after gates (HALT FailStrategy may have set state.halt)
          6. skip-check (handler set phase_log[name][skipped] → SKIPPED)
          7. after_review (Phase 8 HumanReview after-hook — currently no-op)
          8. adapter (auto-fire ctx.session_adapter_registry[step.phase])
          9. checkpoint (ctx.on_checkpoint(step.phase, state))
         10. metrics (ctx.on_metrics(step.phase, state))

        Stage 9 (checkpoint) fires only on COMPLETED — a halted phase is not a
        completed checkpoint (see ``_persist_halted_step``). Adapter + metrics
        (stages 8, 10) fire on COMPLETED and HALTED; adapter ALSO fires on
        SKIPPED for clean-review short-circuit (RoundAdapter writes a
        critique-only round entry).
        """
        # ── 1. before_review (Phase 8 stub) ─────────────────────────────
        # No-op until Phase 8 HumanReview backend wires here.

        # ── 2. execute via PhaseStepExecutor ────────────────────────────
        executor = self._resolve_executor(step.execution, ctx)
        try:
            state = executor.execute(step, state, ctx)
        except Exception as exc:
            return StepOutcome(
                status=StepStatus.FAILED, state=state,
                reason=f"{type(exc).__name__}: {exc}",
                error=exc,
            )

        # ── 3. halt-check ───────────────────────────────────────────────
        # NB: HALT short-circuits gates but STILL fires adapter and metrics
        # (NOT checkpoint — a halted phase is not a completed checkpoint).
        # Handler-side HALT often happens after an agent returned output
        # (e.g. PLAN parse failure), so usage/time must be accounted even
        # though the profile stops.
        if state.halt:
            self._persist_halted_step(step, state, ctx)
            return StepOutcome(
                status=StepStatus.HALTED, state=state,
                reason=state.halt_reason or "halt",
            )

        # ── 4. gates (only when handler didn't skip) ────────────────────
        log = state.phase_log.get(step.phase, {})
        skipped = isinstance(log, dict) and bool(log.get("skipped"))
        if not skipped and step.quality_gates:
            self._fire_gates(step, state, ctx)

        # ── 5. halt-check after gates (HALT FailStrategy) ───────────────
        if state.halt:
            self._persist_halted_step(step, state, ctx)
            return StepOutcome(
                status=StepStatus.HALTED, state=state,
                reason=state.halt_reason or "halt-after-gate",
            )

        # ── 6. skip-check ───────────────────────────────────────────────
        # Re-read log after gates may have mutated it.
        log = state.phase_log.get(step.phase, {})
        skipped = isinstance(log, dict) and bool(log.get("skipped"))

        # ── 7. after_review (Phase 8 stub) ──────────────────────────────
        # No-op until Phase 8 HumanReview backend wires here.

        # ── 8. adapter (fires on COMPLETED + SKIPPED for legacy parity) ─
        self._fire_adapter(step, state, ctx)

        # ── 9. checkpoint (skip for skipped phases — legacy parity) ─────
        if not skipped and ctx.on_checkpoint is not None:
            ctx.on_checkpoint(step.phase, state)

        # ── 10. metrics (skip for skipped — handler didn't run agents) ──
        if not skipped and ctx.on_metrics is not None:
            ctx.on_metrics(step.phase, state)

        if skipped:
            return StepOutcome(
                status=StepStatus.SKIPPED, state=state,
                reason=str(log.get("skipped")),
            )
        return StepOutcome(status=StepStatus.COMPLETED, state=state)

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _persist_halted_step(
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> None:
        """Persist data produced before a HALT — but NOT a completion checkpoint.

        Invariant: a halted phase is not a completed checkpoint. Checkpoint
        completeness equals a *successful* outcome, never the mere presence of a
        ``phase.end``. Recording a halted phase via ``ctx.on_checkpoint`` would
        add it to ``ckpt.completed``, which both resume consumers read
        (``session_run.resume_completed_phases`` and
        ``run_setup._peek_completed_phases``) — a halted IMPLEMENT would then be
        skipped on resume and the run would march on to review against partial
        work. So the halt seam fires adapter (writes session shape) and metrics
        (the halt may follow an expensive agent call — PLAN parse failure, QA
        rejection, gate HALT) but deliberately omits the checkpoint.
        """
        PhaseLifecycle._fire_adapter(step, state, ctx)
        if ctx.on_metrics is not None:
            ctx.on_metrics(step.phase, state)

    @staticmethod
    def _resolve_executor(
        execution: str, ctx: LifecycleContext,
    ) -> PhaseStepExecutor:
        """Resolve the PhaseStepExecutor for ``step.execution``. Falls
        back to a freshly-built default registry when ``ctx.execution_mode_registry``
        is None — keeps the FSM testable in isolation without forcing
        callers to pre-build the registry."""
        registry = ctx.execution_mode_registry
        if registry is None:
            registry = default_execution_mode_registry()
        return registry.get(execution)

    @staticmethod
    def _fire_gates(
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> None:
        """Delegate to the existing ``runtime._fire_step_quality_gates``
        helper. Phase 5e-5 substep 2 keeps the gate-firing logic where
        it lives today (runtime.py); substep 6 audits whether to move
        it into lifecycle.py for cleaner separation."""
        # Lazy import — runtime depends on lifecycle types.
        from pipeline.runtime import _fire_step_quality_gates
        _fire_step_quality_gates(
            step, state,
            quality_gate_registry=ctx.quality_gate_registry,
        )

    @staticmethod
    def _fire_adapter(
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> None:
        """Auto-fire the registered SessionAdapter for ``step.phase``.
        Adapter writes ``state.phase_log[step.phase]`` → session shape.

        round_n resolution mirrors
        ``_PipelineRun._round_n_for_adapter`` — prefers
        ``state.extras["_active_loop_round_key"]`` (set by
        ``runtime._run_loop_step``) and reads the round counter via
        that key; falls back to phase-name convention table for direct
        tests / isolation cases.

        ``ctx.run_config["session"]`` carries the orchestrator's
        session dict — substep 4 wires this; substep 2 tests that
        construct ctx without session get a no-op (adapter doesn't
        fire).
        """
        if ctx.session_adapter_registry is None:
            return
        adapter = ctx.session_adapter_registry.get_or_none(step.phase)
        if adapter is None:
            return
        session = ctx.run_config.get("session")
        if session is None:
            return
        round_n = _resolve_round_n(step.phase, state)
        adapter.write(step.phase, state, session, round_n=round_n)


def _resolve_round_n(phase_name: str, state: Any) -> int | None:
    """Resolve the loop round_n relevant to ``phase_name`` for adapter
    calls. Mirrors ``_PipelineRun._round_n_for_adapter`` (lifted to
    module scope in substep 4 so FSM doesn't have to round-trip
    through orchestrator).

    Priority:
      1. ``state.extras["_active_loop_round_key"]`` (typed pointer set
         by ``runtime._run_loop_step``) → read counter via that key
      2. Phase-name convention table (plan/validate_plan → plan_round;
         review_changes/repair_changes → repair_round; else loop_round)
      3. None when no loop context available
    """
    active_key = state.extras.get("_active_loop_round_key")
    if isinstance(active_key, str) and active_key:
        v = state.extras.get(active_key)
        if v is not None:
            return int(v)
    by_phase = {
        "plan":           "plan_round",
        "validate_plan":  "plan_round",
        "review_changes": "repair_round",
        "repair_changes": "repair_round",
    }
    fallback_key = by_phase.get(phase_name, "loop_round")
    v = state.extras.get(fallback_key)
    return int(v) if v is not None else None
