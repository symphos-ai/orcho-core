"""
pipeline/runtime/runner.py — data-driven pipeline runner.

Phase 5d made the v2 ``Profile`` shape the active dispatch input:
``Profile.steps`` contains ``PhaseStep`` entries and ``LoopStep`` retry
blocks. ``run_profile`` walks that declarative recipe and dispatches
registered phase handlers by name.

Important Phase 5e-5 boundaries:

  - Handlers receive and return a ``PipelineState``. Returning is optional:
    handlers may mutate in place and return None — the runner falls back
    to the input state.
  - Production ``Profile`` dispatch now routes ``PhaseStep`` entries
    through the Phase 1.5 ``StepOutcome`` lifecycle FSM. State still
    carries transitional ``halt`` / ``halt_reason`` flags until the
    Phase 5e-5 substep-6 state.extras audit removes the remaining
    shims.
  - ``PhaseStep.execution`` and ``PhaseStep.quality_gates`` are active:
    execution resolves through ``LifecycleContext.execution_mode_registry``
    (built-in: ``linear``; plugins register more), and gates fire inside the FSM.
  - ``PipelineProfile`` remains only as a legacy in-process helper for
    direct dispatcher tests / inline phase dispatch. The v1 JSON loader
    and ``_config/pipeline_profiles.json`` were removed in Phase 5d-5.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pipeline.runtime.handoff import (
    HUMAN_DIRECTED_FLAG_KEY,
    HandoffOutcomeKind,
    PhaseHandoffOutcomeCallback,
    PhaseHandoffResolution,
    PhaseHandoffResolver,
    build_phase_handoff_signal,
    describe_handoff_outcome,
    extra_human_directed_rounds,
    pause_resolver,
)
from pipeline.runtime.profile import LoopStep, PipelineProfile, Profile
from pipeline.runtime.results import PhaseRegistry
from pipeline.runtime.roles import PhaseHandoffType
from pipeline.runtime.state import PipelineState
from pipeline.runtime.steps import PhaseStep

if TYPE_CHECKING:
    from pipeline.quality_gates import QualityGateResult


#: Pre-phase skip channel (ADR 0086, correction routing). An
#: ``on_phase_pre`` callback may set ``state.extras[PHASE_PRE_SKIP_KEY]`` to
#: a non-empty reason string to mark the phase it is entering as not
#: applicable. The runner consumes (pops) the key exactly once, immediately
#: after the callback returns, and — when no stronger halt/handoff is
#: pending — skips the phase via ``_skip_phase`` (no handler / FSM / gate /
#: adapter / checkpoint / metrics, mirroring the resume-skip path).
PHASE_PRE_SKIP_KEY = "_phase_pre_skip_reason"

#: Skip-end context (ADR 0086). While ``_skip_phase`` fires ``on_phase_end``
#: for a skipped phase, ``state.extras[PHASE_END_SKIPPED_KEY]`` holds that
#: phase's name so the callback can tell a skip-end from a real phase-end —
#: a skipped phase performed no work, so post-phase hooks (``after_phase``
#: verification gates) must not run for it. The key lives only for the
#: duration of the callback: it is set right before ``on_phase_end`` and
#: removed in a ``finally`` immediately after, so it can never leak into a
#: later phase. Reading a stale ``skipped`` marker from the phase log is NOT
#: an alternative — loop phases legitimately leave handler-side skip records
#: in earlier rounds and still execute later ones.
PHASE_END_SKIPPED_KEY = "_phase_end_skipped"


def run_profile(
    profile: PipelineProfile | Profile,
    state: PipelineState,
    registry: PhaseRegistry,
    *,
    on_phase_start: Callable[[str, PipelineState], None] | None = None,
    on_phase_end:   Callable[[str, PipelineState], None] | None = None,
    on_round_end:   Callable[[LoopStep, int, PipelineState], None] | None = None,
    quality_gate_registry: Any = None,
    ctx: Any = None,  # LifecycleContext — Phase 5e-5 substep 3
    completed_phases: set[str] | None = None,
    phase_handoff_resolver: PhaseHandoffResolver | None = None,
    on_handoff_outcome: PhaseHandoffOutcomeCallback | None = None,
    on_phase_pre: Callable[[str, PipelineState], None] | None = None,
) -> PipelineState:
    """Walk profile entries in order, dispatching each.

    Accepts both shapes:
      * Legacy ``PipelineProfile`` (``phases: tuple[str | LoopStep, ...]``)
        — dispatches by phase name, str entries run their handler once,
        LoopStep entries iterate inner phases. Retained only for direct
        in-process dispatcher tests.
      * Phase 5 redesign ``Profile`` (``steps: tuple[PhaseStep | LoopStep, ...]``)
        — dispatches each PhaseStep by ``step.phase`` name, LoopStep
        entries iterate inner PhaseSteps. Per-step ``execution`` /
        ``quality_gates`` / ``human_review`` consumed by the lifecycle FSM
        when ``ctx`` is provided.

    The ``on_phase_*`` callbacks fire around every handler invocation
    (including loop iterations) — the orchestrator uses them to wire
    banners, event-store emissions, and checkpoint persistence without
    the runtime needing to know about them.

    Phase 5c step 6: ``on_round_end`` fires once per ``LoopStep``
    iteration AFTER the inner phases of that round complete (whether
    via natural completion, early halt, or until-satisfied). Callback
    signature: ``(loop_step, round_n, state) -> None``. Used by the
    orchestrator to mid-loop ``save_session`` per round (legacy
    ``run_review_fix_loop`` checkpoint behaviour).

    Phase 5e-5 substep 3: ``ctx`` parameter accepts a
    ``pipeline.lifecycle.LifecycleContext``. When provided, ``PhaseStep``
    entries dispatch via ``PhaseLifecycle.execute_step(step, state, ctx)``
    (typed FSM). When ``ctx`` is None, ``PhaseStep`` entries still
    resolve their executor from the lifecycle registry — see
    ``_dispatch_one`` — but without FSM bookkeeping (gates fire
    inline, no adapter/checkpoint/metrics callbacks).

    Phase 5e-5 substep 6: legacy entry-name composite registry
    (``pipeline.execution_modes.default_execution_modes_registry``)
    DELETED. v1 entry-name dispatch (``["dag", "final_acceptance"]``) is no
    longer reachable; the only execution-mode dispatch is at the
    PhaseStep level via ``LifecycleContext.execution_mode_registry``.

    Phase 5e-5 substep 6b: when ``profile`` is a v2 ``Profile`` and
    ``ctx`` is None, build a minimal default ``LifecycleContext`` so
    every PhaseStep entry routes through the FSM. Tests calling
    ``run_profile(profile, state, registry)`` without ctx now get
    ctx-driven dispatch automatically; the legacy ``_dispatch_one``
    path survives only for ``PipelineProfile`` str entries (direct
    in-process helper calls).

    Resume contract (``completed_phases``): when the orchestrator
    resumes from a checkpoint, it passes the set of phase names that
    already finished in the prior run. Top-level ``PhaseStep`` entries
    whose ``step.phase`` is in that set are short-circuited — the
    handler does not re-execute, but ``on_phase_start`` /
    ``on_phase_end`` still fire for trace continuity and the phase
    log records ``{"skipped": "completed earlier in this run (resumed)"}``.
    LoopStep inner phases are NOT auto-skipped: a loop owns its own
    iteration semantics and must replay from a coherent state, which
    is a separate concern (see the resume brief for the loop-aware
    follow-up). Without ``completed_phases`` this argument is inert,
    so the fresh-run path is byte-for-byte unchanged.

    Pre-phase skip contract (``PHASE_PRE_SKIP_KEY``, ADR 0086): the
    ``on_phase_pre`` callback may mark the phase it is about to enter as
    not applicable by setting ``state.extras[PHASE_PRE_SKIP_KEY]`` to a
    non-empty reason string. The runner consumes that key exactly once —
    it is popped immediately after the callback returns, before the
    halt / handoff check, so the key never survives the phase. ``halt``
    and a pending ``phase_handoff_request`` outrank a skip request (the
    popped reason is discarded and the run breaks). Otherwise a non-empty
    reason skips the phase via ``_skip_phase`` (no handler / FSM / gate /
    adapter / checkpoint / metrics — parity with the resume-skip path).
    The mechanism was added for correction routing; it is inert for every
    caller that does not pass ``on_phase_pre``.
    """
    _completed_phases: set[str] = set(completed_phases or ())
    resolver: PhaseHandoffResolver = phase_handoff_resolver or pause_resolver
    # Phase 5d: Profile is the production shape. PipelineProfile remains
    # only for private inline helper dispatch / direct runtime tests.
    if isinstance(profile, Profile):
        entries = profile.steps
        if profile.implementation_execution is not None:
            state.extras["implementation_execution"] = (
                profile.implementation_execution.value
            )
        # Phase 5e-5 substep 6b: ensure ctx is always populated for
        # v2 Profile dispatch. Auto-built default ctx is cheap (~3
        # default-helper instances) and gives the FSM a uniform call
        # site regardless of caller.
        if ctx is None:
            from pipeline.lifecycle import default_lifecycle_context
            ctx = default_lifecycle_context(
                phase_registry=registry,
                quality_gate_registry=quality_gate_registry,
            )
        # Profile.__post_init__ already validated. We still cross-check
        # phase-name registration against the runtime registries.
        _validate_v2_entries(
            entries,
            registry,
            profile.name,
            phase_execution_registry=ctx.execution_mode_registry,
        )
        # Phase handoff support matrix (slice 2): non-bypass handoff is
        # only honoured for PhaseSteps nested inside a LoopStep. A
        # top-level PhaseStep with a non-bypass policy is rejected here,
        # before any handler runs, so callers see a clean error from
        # ``run_profile`` instead of a stale verdict mid-run.
        _validate_handoff_support(entries, profile.name)
    else:
        # Legacy PipelineProfile path: validate as before. ctx stays
        # None — str entries dispatch via _dispatch_one without FSM
        # bookkeeping.
        profile.validate(registry)
        entries = profile.phases

    for entry in entries:
        if state.halt:
            break
        if isinstance(entry, LoopStep):
            if _completed_phases:
                loop_phases = _loop_phase_names(entry)
                completed_inside_loop = loop_phases & _completed_phases
                if completed_inside_loop:
                    if completed_inside_loop == loop_phases:
                        # Loop finished cleanly in a prior dispatch (every
                        # inner phase was committed). Skip the LoopStep
                        # entirely — same resume semantic as
                        # ``_skip_completed_phase`` for top-level
                        # PhaseSteps. Emit per-inner-phase skip records so
                        # banner/trace channels stay coherent.
                        for inner in entry.steps:
                            if isinstance(inner, PhaseStep):
                                _skip_completed_phase(
                                    inner, state,
                                    on_phase_start=on_phase_start,
                                    on_phase_end=on_phase_end,
                                )
                        continue
                    # Partial overlap is the genuinely unsafe mid-loop
                    # case the original guard was written for: round
                    # identity is needed to resume safely, and that
                    # checkpoint shape doesn't exist yet.
                    raise RuntimeError(
                        "Cannot safely resume loop-internal completed phases "
                        f"{sorted(completed_inside_loop)}: round-level checkpoint "
                        "resume is not implemented yet. Use a profile that "
                        "starts after the loop, or rerun without --resume "
                        "after confirming replay is safe."
                    )
            state = _run_loop_step(
                entry, state, registry,
                on_phase_start=on_phase_start, on_phase_end=on_phase_end,
                on_round_end=on_round_end,
                quality_gate_registry=quality_gate_registry,
                ctx=ctx,
                phase_handoff_resolver=resolver,
                on_handoff_outcome=on_handoff_outcome,
                on_phase_pre=on_phase_pre,
            )
            continue
        if isinstance(entry, PhaseStep):
            # Resume short-circuit: when the orchestrator's checkpoint
            # already records this phase as completed in a prior run,
            # do NOT re-execute the handler. Write phases like
            # ``implement`` mutate the working tree, so silently
            # re-running on resume would clobber the user's progress.
            if entry.phase in _completed_phases:
                _skip_completed_phase(
                    entry, state,
                    on_phase_start=on_phase_start,
                    on_phase_end=on_phase_end,
                )
                continue
            # Pre-phase seam (ADR 0081): the orchestrator may evaluate a
            # ``before_phase`` verification gate here, *before* the handler
            # runs, so a require gate can abort/handoff before entering the
            # phase (the FSM halt-check fires only after the handler executes,
            # so this is the single point that can pre-empt a phase). Default
            # ``None`` keeps every other caller byte-identical.
            if on_phase_pre is not None:
                on_phase_pre(entry.phase, state)
                # Pre-phase skip channel (ADR 0086): consume-once. Pop the
                # key UNCONDITIONALLY and immediately, before any halt /
                # handoff check, so a stale reason can never survive the
                # phase and skip an unrelated one later.
                skip_reason = state.extras.pop(PHASE_PRE_SKIP_KEY, None)
                # Halt / handoff outrank a skip request: an on_phase_pre that
                # both halted and asked to skip stops the run here (the popped
                # skip_reason is simply discarded).
                if state.halt or state.phase_handoff_request is not None:
                    break
                if isinstance(skip_reason, str) and skip_reason:
                    _skip_phase(
                        entry, state, skip_reason,
                        on_phase_start=on_phase_start,
                        on_phase_end=on_phase_end,
                    )
                    continue
            # Phase 5e-5 substep 6b: ctx is always populated for v2
            # Profile dispatch (auto-built above when caller passed
            # ctx=None). FSM owns the gate / adapter / checkpoint /
            # metrics stages; ``on_phase_start`` fires for banner /
            # timer / current-phase tracking.
            state = _dispatch_via_fsm(
                entry, state, ctx,
                on_phase_start=on_phase_start,
                on_phase_end=on_phase_end,
            )
            if state.phase_handoff_request is not None:
                break
            continue
        # Plain phase name — legacy entry shape (PipelineProfile only;
        # ctx stays None on this path).
        state = _dispatch_one(
            entry, state, registry,
            on_phase_start=on_phase_start, on_phase_end=on_phase_end,
            quality_gate_registry=quality_gate_registry,
        )

    return state


_SUPPORTED_PLAN_HANDOFF_PHASE = "validate_plan"
_SUPPORTED_PLAN_HANDOFF_PRECEDING_PHASE = "plan"
_SUPPORTED_PLAN_HANDOFF_UNTIL_PHASE = "validate_plan"
_SUPPORTED_PLAN_HANDOFF_UNTIL_FIELD = "approved"
_SUPPORTED_REPAIR_HANDOFF_PHASE = "review_changes"
_SUPPORTED_REPAIR_HANDOFF_FOLLOWING_PHASE = "repair_changes"
_SUPPORTED_REPAIR_HANDOFF_UNTIL_PHASE = "review_changes"
_SUPPORTED_REPAIR_HANDOFF_UNTIL_FIELD = "clean"
# ADR 0073: the implement phase is a bare top-level PhaseStep (no enclosing
# review loop, no ``until`` predicate). Its handoff fires from the subtask_dag
# substance-repair fallback, not a verdict loop.
_SUPPORTED_IMPLEMENT_HANDOFF_PHASE = "implement"

# ADR 0112 §5: final_acceptance is the scope-expansion sanction seam. A
# scope-expansion ``HANDOFF`` is raised at runtime by the gate routing (not a
# verdict loop), so — like implement — it is a bare top-level PhaseStep with no
# enclosing loop and no ``until`` predicate. This constant is the conscious twin
# of ``handoff._SUPPORTED_HANDOFF_PHASES``'s ``final_acceptance`` entry: widening
# support is a deliberate change at both sites.
_SUPPORTED_SCOPE_EXPANSION_HANDOFF_PHASE = "final_acceptance"


def _is_validate_plan_loop_until(predicate: str) -> bool:
    """Return True iff ``predicate`` is the canonical
    ``validate_plan.approved`` form (no negation, no whitespace surprises).

    Non-canonical forms (``not validate_plan.approved``,
    ``review_changes.has_issues``, etc.) are rejected by
    :func:`_validate_handoff_support` so the runtime slice stays bounded
    to single-project validate_plan plan loops.
    """
    p = predicate.strip()
    if p.lower().startswith("not "):
        return False
    phase, sep, field = p.partition(".")
    if not sep:
        return False
    return (
        phase.strip() == _SUPPORTED_PLAN_HANDOFF_UNTIL_PHASE
        and field.strip() == _SUPPORTED_PLAN_HANDOFF_UNTIL_FIELD
    )


def _is_review_repair_loop_until(predicate: str) -> bool:
    """Return True iff ``predicate`` is the canonical
    ``review_changes.clean`` form (no negation)."""
    p = predicate.strip()
    if p.lower().startswith("not "):
        return False
    phase, sep, field = p.partition(".")
    if not sep:
        return False
    return (
        phase.strip() == _SUPPORTED_REPAIR_HANDOFF_UNTIL_PHASE
        and field.strip() == _SUPPORTED_REPAIR_HANDOFF_UNTIL_FIELD
    )


def _validate_handoff_support(
    entries: tuple,
    profile_name: str,
    *,
    enclosing_loop: LoopStep | None = None,
) -> None:
    """Enforce slice-1 runtime support matrix for ``PhaseStep.handoff``.

    Runtime supports non-bypass handoff in two loop shapes plus one
    bare top-level phase:

    * ``plan → validate_plan`` with ``until: validate_plan.approved``;
    * ``review_changes → repair_changes`` with
      ``until: review_changes.clean``;
    * bare top-level ``implement`` (ADR 0073) — no enclosing loop, no
      ``until``; only ``human_feedback_on_reject`` is accepted, and its
      substance-repair fallback fires from the subtask_dag hook.

    Every other shape is rejected at ``run_profile`` time — before any
    handler runs — so callers see a clean error instead of a
    silently-wrong pause mid-run. The loader stays intentionally generic
    (parses any handoff on any step); the phase-name and loop-shape
    checks live here so future runtime slices can widen support without
    re-touching the parser.

    A nested ``LoopStep`` is walked recursively with its own
    ``enclosing_loop`` so the rule applies uniformly at any depth.
    """
    enclosing_steps = enclosing_loop.steps if enclosing_loop is not None else ()
    for entry_index, entry in enumerate(entries):
        if isinstance(entry, PhaseStep):
            policy = entry.handoff
            if policy is None or policy.type is PhaseHandoffType.HUMAN_BYPASS:
                continue
            if entry.phase == _SUPPORTED_IMPLEMENT_HANDOFF_PHASE:
                # ADR 0073: implement is a bare top-level step — no enclosing
                # loop, no ``until`` predicate. The substance-repair fallback
                # fires from the subtask_dag hook rather than a verdict loop,
                # so we validate the handoff type only; repair_attempts and
                # on_exhausted are already validated by
                # ``PhaseHandoffPolicy.__post_init__``.
                if enclosing_loop is not None:
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r} inside a "
                        "LoopStep, but implement handoff is only supported as a "
                        "bare top-level step. Move it out of the loop or use "
                        "'human_bypass'."
                    )
                if policy.type is not PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT:
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r}; "
                        "implement handoff only supports "
                        "'human_feedback_on_reject'."
                    )
                continue
            if entry.phase == _SUPPORTED_SCOPE_EXPANSION_HANDOFF_PHASE:
                # ADR 0112 §5: final_acceptance is the scope-expansion sanction
                # seam. Its handoff is raised at runtime by the gate routing
                # rather than a verdict loop, so — like implement — it is only
                # supported as a bare top-level step; declaring it inside a
                # LoopStep is rejected. The runtime-raised signal builder
                # (``build_scope_expansion_handoff_signal``) owns the
                # trigger/action shape, so the type is not constrained here.
                if enclosing_loop is not None:
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r} inside a "
                        "LoopStep, but final_acceptance (scope-expansion) "
                        "handoff is only supported as a bare top-level step. "
                        "Move it out of the loop or use 'human_bypass'."
                    )
                continue
            if enclosing_loop is None:
                raise ValueError(
                    f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                    f"declares handoff type {policy.type.value!r} but is "
                    "not inside a LoopStep. Phase handoffs are only "
                    "supported for bounded review loops. Wrap the step "
                    "in a supported loop or use "
                    "'human_bypass'."
                )
            if entry.phase == _SUPPORTED_PLAN_HANDOFF_PHASE:
                if not _is_validate_plan_loop_until(enclosing_loop.until):
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r}; its "
                        "enclosing LoopStep.until is "
                        f"{enclosing_loop.until!r}, but validate_plan "
                        "handoff requires until: 'validate_plan.approved'. "
                        "Use 'human_bypass' on unsupported loops."
                    )
                has_plan_predecessor = any(
                    isinstance(other, PhaseStep)
                    and other.phase == _SUPPORTED_PLAN_HANDOFF_PRECEDING_PHASE
                    for other in enclosing_steps[:entry_index]
                )
                if not has_plan_predecessor:
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r} but its "
                        "enclosing LoopStep does not declare a "
                        f"PhaseStep(phase={_SUPPORTED_PLAN_HANDOFF_PRECEDING_PHASE!r}) "
                        "before it. retry_feedback resume needs a 'plan' "
                        "predecessor to inject feedback into."
                    )
                continue
            if entry.phase == _SUPPORTED_REPAIR_HANDOFF_PHASE:
                if policy.type is not PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT:
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r}; "
                        "review_changes handoff only supports "
                        "'human_feedback_on_reject'."
                    )
                if not _is_review_repair_loop_until(enclosing_loop.until):
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r}; its "
                        "enclosing LoopStep.until is "
                        f"{enclosing_loop.until!r}, but review handoff "
                        "requires until: 'review_changes.clean'."
                    )
                has_repair_successor = any(
                    isinstance(other, PhaseStep)
                    and other.phase == _SUPPORTED_REPAIR_HANDOFF_FOLLOWING_PHASE
                    for other in enclosing_steps[entry_index + 1:]
                )
                if not has_repair_successor:
                    raise ValueError(
                        f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                        f"declares handoff type {policy.type.value!r} but its "
                        "enclosing LoopStep does not declare a "
                        f"PhaseStep(phase={_SUPPORTED_REPAIR_HANDOFF_FOLLOWING_PHASE!r}) "
                        "after it. retry_feedback resume needs a repair "
                        "successor to apply feedback before re-review."
                    )
                continue
            raise ValueError(
                f"profile {profile_name!r}: PhaseStep {entry.phase!r} "
                f"declares handoff type {policy.type.value!r} but phase "
                "handoffs are only supported on 'validate_plan', "
                "'review_changes', 'implement', and 'final_acceptance'. Use "
                "'human_bypass' on other phases."
            )
        elif isinstance(entry, LoopStep):
            _validate_handoff_support(
                entry.steps, profile_name, enclosing_loop=entry,
            )


def _loop_phase_names(step: LoopStep) -> set[str]:
    """Return every phase name nested under *step*.

    Resume skip is safe for top-level ``PhaseStep`` entries because the
    dispatcher can omit the whole phase. Loop bodies need round identity
    before they can be resumed safely; until that exists, completed
    loop members must fail closed instead of replaying mutating work.
    """
    names: set[str] = set()
    for inner in step.steps:
        if isinstance(inner, PhaseStep):
            names.add(inner.phase)
        elif isinstance(inner, LoopStep):
            names.update(_loop_phase_names(inner))
    return names


def _skip_phase(
    step: PhaseStep,
    state: PipelineState,
    reason: str,
    *,
    on_phase_start: Callable[[str, PipelineState], None] | None,
    on_phase_end:   Callable[[str, PipelineState], None] | None,
) -> None:
    """Mark a phase as skipped in the phase log + emit START/END callbacks.

    The handler does not run, so adapter / checkpoint / metrics do NOT
    fire for the skip. ``on_phase_start`` / ``on_phase_end`` still fire so
    the orchestrator's banner + trace channels see a coherent phase pair.
    While ``on_phase_end`` runs, ``state.extras[PHASE_END_SKIPPED_KEY]``
    names the skipped phase so the callback can suppress post-phase work
    (``after_phase`` gates) that only applies to executed phases; the key
    is removed immediately after the callback returns.

    The phase log entry uses the same ``skipped`` key the lifecycle FSM
    recognises (``pipeline.lifecycle._execute_step_body`` stage 6) so any
    downstream consumer that already handles handler-side skip records
    (e.g. session adapters) sees a consistent shape. ``reason`` is the
    operator-facing skip cause — resume-skip and the correction pre-phase
    route (ADR 0086) pass different strings here.
    """
    if on_phase_start is not None:
        on_phase_start(step.phase, state)
    state.phase_log.setdefault(step.phase, {})
    log_entry = state.phase_log[step.phase]
    if isinstance(log_entry, dict):
        log_entry.setdefault("skipped", reason)
    if on_phase_end is not None:
        state.extras[PHASE_END_SKIPPED_KEY] = step.phase
        try:
            on_phase_end(step.phase, state)
        finally:
            state.extras.pop(PHASE_END_SKIPPED_KEY, None)


#: Resume-skip reason (a phase already committed earlier in this run).
#: Public so the render seam can gate the resume-summary line on this exact
#: reason without duplicating the literal (see
#: ``pipeline.project.profile_dispatch.emit_phase_log_end``).
RESUME_SKIP_REASON = "completed earlier in this run (resumed)"


def _skip_completed_phase(
    step: PhaseStep,
    state: PipelineState,
    *,
    on_phase_start: Callable[[str, PipelineState], None] | None,
    on_phase_end:   Callable[[str, PipelineState], None] | None,
) -> None:
    """Resume-skip a phase that already completed in a prior run.

    Thin wrapper over :func:`_skip_phase` with the resume reason —
    operators expect a "phase X completed (skipped on resume)" line.
    """
    _skip_phase(
        step, state, RESUME_SKIP_REASON,
        on_phase_start=on_phase_start,
        on_phase_end=on_phase_end,
    )


def _dispatch_via_fsm(
    step: PhaseStep,
    state: PipelineState,
    ctx: Any,  # LifecycleContext
    *,
    on_phase_start: Callable[[str, PipelineState], None] | None,
    on_phase_end:   Callable[[str, PipelineState], None] | None,
) -> PipelineState:
    """Phase 5e-5 substep 3: dispatch one ``PhaseStep`` through the
    lifecycle FSM and translate the typed ``StepOutcome`` back to
    legacy ``state.halt`` semantics.

    Legacy ``on_phase_start`` / ``on_phase_end`` callbacks still fire
    around the FSM call so banners + ``_current_phase`` tracking +
    other transitional channels keep working. Substep 6 cleans these
    up once handlers fully migrate to ctx.

    The FSM stages (gates / adapter / checkpoint / metrics) run
    INSIDE ``PhaseLifecycle.execute_step`` via the ``ctx`` callbacks;
    ``on_phase_end`` here just gets the legacy banner-END side-effect.
    """
    from pipeline.lifecycle import PhaseLifecycle, StepStatus

    if on_phase_start is not None:
        on_phase_start(step.phase, state)

    fsm = PhaseLifecycle()
    outcome = fsm.execute_step(step, state, ctx)
    state = outcome.state

    if outcome.status is StepStatus.HALTED:
        if not state.halt:
            state.stop(outcome.reason or "halt")
    elif outcome.status is StepStatus.FAILED:
        # Re-raise as exception with original phase context — preserves
        # legacy ``_safe_phase`` / ``_record_phase_failure`` semantics.
        # Prefer the original exception so operator-facing errors keep
        # their type across the FSM boundary.
        if outcome.error is not None:
            raise outcome.error
        raise RuntimeError(outcome.reason or f"phase {step.phase!r} failed")

    if on_phase_end is not None:
        on_phase_end(step.phase, state)
    return state


def _validate_v2_entries(
    entries: tuple,
    registry: PhaseRegistry,
    profile_name: str,
    *,
    phase_execution_registry: Any = None,
) -> None:
    """Raise ValueError if any entry references an unregistered phase
    or an unsupported ``PhaseStep.execution`` value.

    Walks both top-level PhaseStep and inner LoopStep PhaseSteps. A
    phase name is "known" when it's in ``PhaseRegistry``.

    ``PhaseStep.execution`` validates against the lifecycle execution
    registry, not a hardcoded set. ``"linear"`` dispatches via
    ``LinearPhaseStepExecutor`` (single handler call); any execution mode
    not registered (built-in or plugin) is rejected generically. Policy-owned
    subtask delivery is selected via ``implementation_execution=subtask_dag``
    on the implement handler, not as a profile-step execution mode.
    """
    if phase_execution_registry is None:
        from pipeline.lifecycle import default_execution_mode_registry
        phase_execution_registry = default_execution_mode_registry()

    def _execution_known(name: str) -> bool:
        has = getattr(phase_execution_registry, "has", None)
        if callable(has):
            return bool(has(name))
        return name in _PHASESTEP_EXECUTION_SUPPORTED

    def _execution_names() -> list[str]:
        names = getattr(phase_execution_registry, "names", None)
        if callable(names):
            return list(names())
        return sorted(_PHASESTEP_EXECUTION_SUPPORTED)

    missing_phase: list[str] = []
    bad_execution: list[tuple[str, str]] = []  # (phase, execution)
    for entry in entries:
        if isinstance(entry, PhaseStep):
            if not registry.has(entry.phase):
                missing_phase.append(entry.phase)
            if not _execution_known(entry.execution):
                bad_execution.append((entry.phase, entry.execution))
        elif isinstance(entry, LoopStep):
            for inner in entry.steps:
                if not registry.has(inner.phase):
                    missing_phase.append(inner.phase)
                if not _execution_known(inner.execution):
                    bad_execution.append((inner.phase, inner.execution))
        else:
            raise TypeError(
                f"Profile {profile_name!r}: entry {entry!r} is neither "
                f"PhaseStep nor LoopStep"
            )
    if missing_phase:
        raise ValueError(
            f"Profile {profile_name!r} references unknown phases: {missing_phase}. "
            f"Registered: {registry.names()}"
        )
    if bad_execution:
        raise ValueError(
            f"Profile {profile_name!r}: PhaseStep.execution must be one of "
            f"{_execution_names()}, got "
            f"{bad_execution}. Customer plugins shipping additional "
            f"execution modes must register them in the lifecycle "
            f"ExecutionModeRegistry before dispatch."
        )


# PhaseStep.execution built-ins. Implement subtask delivery is no longer
# selected here; use pipeline.implementation_execution="subtask_dag".
_PHASESTEP_EXECUTION_SUPPORTED: frozenset[str] = frozenset({"linear"})


def _dispatch_one(
    phase_name: str,
    state: PipelineState,
    registry: PhaseRegistry,
    *,
    on_phase_start: Callable[[str, PipelineState], None] | None,
    on_phase_end:   Callable[[str, PipelineState], None] | None,
    step: PhaseStep | None = None,
    quality_gate_registry: Any = None,
) -> PipelineState:
    """Run one entry with surrounding callbacks.

    Phase 5e-5 substep 6b: this helper now serves only the legacy
    ``PipelineProfile`` ``str``-entry path. v2 ``Profile`` /
    ``PhaseStep`` entries always dispatch through the FSM via
    ``_dispatch_via_fsm`` (``run_profile`` auto-builds a default ctx
    when caller passed None). The substep-5 non-linear branch on the
    ctx-less path is no longer reachable from ``run_profile`` and was
    removed.

    ``step`` parameter is retained because legacy ``PipelineProfile``
    helpers may still pass a ``PhaseStep`` instance for its
    ``quality_gates`` declaration; gates fire here for that pre-FSM
    case.
    """
    handler = registry.get(phase_name)
    if on_phase_start is not None:
        on_phase_start(phase_name, state)
    result = handler(state)
    if isinstance(result, PipelineState):
        state = result
    # Phase 5e step 1: fire profile-declared quality gates between
    # handler return and on_phase_end. Skipped handlers don't get
    # gates (mirrors legacy: skipped phases didn't run tests either).
    if step is not None and step.quality_gates:
        log_entry = state.phase_log.get(phase_name) or {}
        skipped = isinstance(log_entry, dict) and bool(log_entry.get("skipped"))
        if not skipped:
            _fire_step_quality_gates(
                step, state,
                quality_gate_registry=quality_gate_registry,
            )
    if on_phase_end is not None:
        on_phase_end(phase_name, state)
    return state


def _fire_step_quality_gates(
    step: PhaseStep,
    state: PipelineState,
    *,
    quality_gate_registry: Any = None,
) -> None:
    """Phase 5e step 1: invoke each ``QualityGate`` declared on the
    ``PhaseStep`` and persist results.

    Resolution: ``state.extras["git_cwd"]`` (legacy convention) →
    ``state.project_dir`` fallback. Each gate runs through the registry
    by name; unregistered gate names raise ``KeyError`` (loud — that's
    a profile bug). Gate handler exceptions are caught inside
    ``run_quality_gate`` and surfaced via ``result.error``; runtime
    keeps moving. ``apply_fail_strategy`` mutates state per
    ``on_fail`` policy (HALT sets state.halt; FEED_INTO_NEXT writes
    to state.extras[gate.feed_target]; TRIGGER_REPLAN sets
    state.last_critique; INFORMATIONAL is logged-only).

    ``quality_gate_registry`` parameter (Phase 5e prep) — DI for
    tests + Phase 5e-5 ``LifecycleContext.quality_gate_registry``
    plumbing. ``None`` falls back to ``default_quality_gate_registry()``
    (production path unchanged). Replaces the previous
    monkey-patch-singleton test pattern.
    """
    # Lazy import — quality_gates depends on runtime types and
    # eager import at module level would create a cycle.
    from pipeline.quality_gates import (
        apply_fail_strategy,
        default_quality_gate_registry,
        run_quality_gate,
    )

    cwd = state.extras.get("git_cwd") or state.project_dir
    registry = (
        quality_gate_registry
        if quality_gate_registry is not None
        else default_quality_gate_registry()
    )
    for gate in step.quality_gates:
        result = run_quality_gate(gate, state, cwd, registry)
        # Persist for audit (Phase 4 convention).
        gate_record = {
            "passed":     result.passed,
            "output":     result.output,
            "duration_s": result.duration_s,
            "kind":       result.kind.value,
            "error":      result.error,
        }
        if result.cost_usd is not None:
            gate_record["cost_usd"] = result.cost_usd
        state.phase_log.setdefault(step.phase, {}).setdefault(
            "quality_gates", {},
        )[gate.name] = gate_record
        # Phase 5e step 2: built-in ``tests`` gate has legacy
        # consumers that expect ``state.extras["last_test_result"]``
        # (orchestrator metrics, repair_changes prompt injection) and
        # ``state.phase_log[name]["test_result"]`` (BuildAdapter /
        # RoundAdapter session shape). Mirror the legacy
        # ``_PipelineRun._on_phase_end`` post-gate stuffing so
        # session shape stays parity-stable through the migration.
        if gate.name == "tests":
            _stuff_legacy_test_result(step.phase, result, state)
        # Strategy mutates state.halt / state.extras / state.last_critique.
        apply_fail_strategy(gate, result, state)
        if state.halt:
            break  # HALT short-circuits remaining gates


def _stuff_legacy_test_result(
    phase_name: str,
    result: QualityGateResult,
    state: PipelineState,
) -> None:
    """Phase 5e step 2: bridge from typed ``QualityGateResult`` to the
    legacy fields BuildAdapter / RoundAdapter / repair_changes-prompt consumers
    expect:

      * ``state.extras["last_test_result"]`` — TestResult-shaped object
        the orchestrator's ``_on_phase_end`` reads to set
        ``state.last_test_output`` (drives repair_changes prompt's
        ``test_failures=`` arg)
      * ``state.phase_log[phase_name]["test_result"]`` — dict shape
        BuildAdapter promotes into session
        ``phases.build.test_result`` / RoundAdapter into
        ``phases.rounds[i].test_result``

    Phase 5e step 5 (``StepOutcome`` FSM) typed-replaces these
    channels; this shim keeps the ad-hoc protocol working until
    that lands.
    """
    from agents.entities import TestResult

    # An empty-output passing result with skipped semantics signals
    # "no run_command configured" — match legacy
    # ``TestResult(skipped=True)``.
    skipped = bool(result.passed and result.output == "")
    tr = TestResult(
        skipped=skipped,
        passed=bool(result.passed),
        output=result.output or "",
        duration=result.duration_s if result.duration_s else 0.0,
    )
    state.extras["last_test_result"] = tr
    if not skipped:
        state.phase_log.setdefault(phase_name, {})["test_result"] = {
            "skipped":  False,
            "passed":   tr.passed,
            "output":   tr.output,
            "duration": tr.duration,
        }
    # state.last_test_output drives repair_changes prompt's ``test_failures``
    # injection (legacy ``_on_phase_end`` line 838). Empty string when
    # tests passed (no failures to surface), otherwise the gate output.
    state.last_test_output = tr.output if (not tr.passed and not tr.skipped) else ""


def _run_loop_step(
    step: LoopStep,
    state: PipelineState,
    registry: PhaseRegistry,
    *,
    on_phase_start: Callable[[str, PipelineState], None] | None,
    on_phase_end:   Callable[[str, PipelineState], None] | None,
    on_round_end:   Callable[[LoopStep, int, PipelineState], None] | None = None,
    quality_gate_registry: Any = None,
    ctx: Any = None,  # LifecycleContext — Phase 5e-5 substep 3
    phase_handoff_resolver: PhaseHandoffResolver | None = None,
    on_handoff_outcome: PhaseHandoffOutcomeCallback | None = None,
    on_phase_pre: Callable[[str, PipelineState], None] | None = None,
) -> PipelineState:
    """Drive a retry loop until ``step.until`` is satisfied or ``step.max_rounds``
    iterations elapse.

    Each round writes the 1-based round number to ``state.extras[step.round_extras_key]``
    so handlers can vary behaviour per round (PLAN flips to replan prompt on
    round ≥ 2, repair_changes escalates to a stronger model, etc.).

    Halt during the loop (e.g. a handler called ``state.stop()`` because of a
    QA gate) breaks the loop and propagates back to ``run_profile``.
    """
    # Phase 5c step 5: stamp max_rounds into extras so handlers can know
    # when they're on the LAST iteration (used by the phase-handoff
    # trigger discipline — pause only after max_rounds without approval,
    # not on first reject). Key naming convention: ``<round_extras_key>_max``.
    #
    # Phase 5 review hardening: also expose the active loop key so
    # orchestrator callbacks can pick the right round counter even after
    # an earlier loop left a different ``*_round`` value in state.extras.
    #
    # Phase 5e-5 substep 6b: auto-build ctx when caller passed None
    # (mirrors ``run_profile`` collapse — every PhaseStep dispatches
    # through the FSM, not the legacy ``_dispatch_one`` path).
    if ctx is None:
        from pipeline.lifecycle import default_lifecycle_context
        ctx = default_lifecycle_context(
            phase_registry=registry,
            quality_gate_registry=quality_gate_registry,
        )
    resolver: PhaseHandoffResolver = phase_handoff_resolver or pause_resolver
    # Phase 2.5 (handoff slice): human-directed extra rounds extend the
    # automatic budget without mutating ``LoopStep.max_rounds``. Slice 4's
    # resume path writes the budget here before re-entering the loop. In
    # slice 2 the value defaults to 0, so behaviour is unchanged for
    # non-resumed runs.
    extra_rounds = extra_human_directed_rounds(state, step)
    total_rounds = step.max_rounds + extra_rounds
    active_key_sentinel = object()
    prev_active_key = state.extras.get("_active_loop_round_key", active_key_sentinel)
    state.extras[f"{step.round_extras_key}_max"] = step.max_rounds
    try:
        state.extras["_active_loop_round_key"] = step.round_extras_key
        for round_n in range(1, total_rounds + 1):
            if state.halt:
                break
            state.extras[step.round_extras_key] = round_n
            state.extras[HUMAN_DIRECTED_FLAG_KEY] = round_n > step.max_rounds
            # Phase 5e step 1 + substep 6b: every inner PhaseStep
            # dispatches through the FSM. Inner step.execution +
            # quality_gates + skill (Phase 7) + human_review (Phase 8)
            # all flow through ``_dispatch_via_fsm``.
            handoff_triggered = False
            deferred_handoff_signal = None
            for inner_step in step.steps:
                if state.halt:
                    break
                # Pre-phase seam (ADR 0081) for loop-inner phases — see the
                # top-level note in ``run_profile``.
                if on_phase_pre is not None:
                    on_phase_pre(inner_step.phase, state)
                    # Pre-phase skip channel (ADR 0086): same consume-once
                    # contract as the top-level seam — pop unconditionally
                    # and immediately, before the halt / handoff check.
                    skip_reason = state.extras.pop(PHASE_PRE_SKIP_KEY, None)
                    if state.halt or state.phase_handoff_request is not None:
                        break
                    if isinstance(skip_reason, str) and skip_reason:
                        _skip_phase(
                            inner_step, state, skip_reason,
                            on_phase_start=on_phase_start,
                            on_phase_end=on_phase_end,
                        )
                        continue
                state = _dispatch_via_fsm(
                    inner_step, state, ctx,
                    on_phase_start=on_phase_start,
                    on_phase_end=on_phase_end,
                )
                if state.halt:
                    break
                # Phase 2 (handoff slice): after every inner phase
                # finishes, check whether its handoff policy fires for
                # this round. The 3-condition gate / always-fire matrix
                # lives in ``build_phase_handoff_signal``; the loop
                # driver owns the resolver dispatch + state mutation.
                #
                # Cross-slice safety: orchestrator-side persistence of
                # ``meta.phase_handoff`` + ``awaiting_phase_handoff``
                # status lands in slice 3. ``_validate_handoff_support``
                # already restricts non-bypass handoff to validate_plan
                # inside the canonical plan loop, and no built-in
                # profile declares such a handoff yet. Plugin profiles
                # that opt in earlier accept a halted-without-meta
                # window until the orchestrator cutover ships.
                # Observability hook (independent of the trigger
                # decision): for every non-bypass handoff policy,
                # ``describe_handoff_outcome`` classifies what the
                # policy did this round — fired / deferred / bypassed
                # / no_verdict. The runner emits it through
                # ``on_handoff_outcome`` so transports (CLI, Web,
                # MCP) can surface the decision even when no pause
                # is requested. Bypass-only policies yield ``None``
                # so observers never see noise for unconfigured
                # phases. Failures inside the observer must not
                # disturb loop state — wrapped in suppress.
                if on_handoff_outcome is not None:
                    outcome = describe_handoff_outcome(
                        inner_step, step, state, round_n,
                    )
                    # ADR 0039 extension: the FIRED outcome for
                    # ``review_changes`` is paired with a deferred
                    # handoff signal that the post-repair re-review
                    # below can clear. Emitting FIRED here would lie
                    # to operator surfaces on the success path
                    # (review reject → repair → re-review approve →
                    # no pause). Suppress the FIRED line here; the
                    # re-review block emits the fresh outcome from
                    # the post-repair verdict instead.
                    if outcome is not None and not (
                        inner_step.phase == "review_changes"
                        and outcome.kind is HandoffOutcomeKind.FIRED
                    ):
                        with contextlib.suppress(Exception):
                            on_handoff_outcome(outcome)

                signal = build_phase_handoff_signal(
                    inner_step, step, state, round_n,
                )
                if signal is None:
                    continue
                if inner_step.phase == "review_changes":
                    # ADR 0039: pause only after the paired
                    # repair_changes step has finished this round.
                    deferred_handoff_signal = signal
                    continue
                resolution = resolver(signal, state)
                if resolution is PhaseHandoffResolution.PAUSE:
                    state.phase_handoff_request = signal
                    state.stop(
                        f"phase handoff requested: {signal.handoff_id}",
                    )
                    handoff_triggered = True
                    break
            # Phase 5c step 6: fire per-round callback AFTER the inner phases
            # of this round complete — including on halt and on until-satisfied.
            # Orchestrator uses this to mid-loop save_session per round (legacy
            # run_review_fix_loop checkpoint behaviour). Failure in callback
            # MUST NOT corrupt loop state — wrap in try/except, log, continue.
            if on_round_end is not None:
                # Observability-only callback. Any failure logged elsewhere
                # (orchestrator's _on_round_end can log); runtime stays
                # robust to checkpoint write failures.
                with contextlib.suppress(Exception):
                    on_round_end(step, round_n, state)
            if handoff_triggered:
                break
            if deferred_handoff_signal is not None and not state.halt:
                # ADR 0039 extension: re-validate the repair before
                # pausing for human feedback. The deferred signal was
                # built from the review verdict BEFORE repair_changes
                # ran on this round; re-dispatch review_changes once
                # more so the verdict (and the handoff payload, if it
                # still fires) reflects the repaired state, not the
                # stale pre-repair critique. ``deferred_handoff_signal``
                # only ever populates on the final automatic round
                # (``build_phase_handoff_signal`` returns ``None`` when
                # ``round_n < loop_step.max_rounds``), so this extra
                # review runs at most once per loop.
                review_step = next(
                    (s for s in step.steps if s.phase == "review_changes"),
                    None,
                )
                if review_step is not None:
                    # Session-continuity flag (ADR 0039 extension):
                    # the re-verify pass audits fixes to its own
                    # prior findings, so it must resume the previous
                    # review session rather than starting cold.
                    # ``_should_resume`` in the review handler honors
                    # this flag for the ``repair_round`` key.
                    state.extras["_review_reverify_resume"] = True
                    try:
                        state = _dispatch_via_fsm(
                            review_step, state, ctx,
                            on_phase_start=on_phase_start,
                            on_phase_end=on_phase_end,
                        )
                    finally:
                        state.extras.pop("_review_reverify_resume", None)
                    if state.halt:
                        break
                    # Emit the (suppressed-in-loop) review_changes
                    # outcome now that the post-repair verdict is
                    # final. This is the operator's single accurate
                    # signal for this round: BYPASSED if the repair
                    # cleared findings, FIRED if it didn't.
                    if on_handoff_outcome is not None:
                        fresh_outcome = describe_handoff_outcome(
                            review_step, step, state, round_n,
                        )
                        if fresh_outcome is not None:
                            with contextlib.suppress(Exception):
                                on_handoff_outcome(fresh_outcome)
                    if _evaluate_until(step.until, state):
                        # Fresh review approved — drop the stale signal
                        # and exit cleanly via the loop's until clause.
                        break
                    rebuilt = build_phase_handoff_signal(
                        review_step, step, state, round_n,
                    )
                    deferred_handoff_signal = rebuilt
                if deferred_handoff_signal is not None:
                    resolution = resolver(deferred_handoff_signal, state)
                    if resolution is PhaseHandoffResolution.PAUSE:
                        state.phase_handoff_request = deferred_handoff_signal
                        state.stop(
                            "phase handoff requested: "
                            f"{deferred_handoff_signal.handoff_id}",
                        )
                        break
            if _evaluate_until(step.until, state):
                break
    finally:
        if prev_active_key is active_key_sentinel:
            state.extras.pop("_active_loop_round_key", None)
        else:
            state.extras["_active_loop_round_key"] = prev_active_key
        state.extras.pop(HUMAN_DIRECTED_FLAG_KEY, None)
    return state


def _evaluate_until(predicate: str, state: PipelineState) -> bool:
    """Read ``predicate`` against ``state.phase_log`` and return True when the
    loop should exit.

    Forms supported:

      * ``"<phase>.<field>"`` — exit when ``state.phase_log[phase][field]``
        is the literal ``True``. Most common: ``"validate_plan.approved"``.
      * ``"not <phase>.<field>"`` — exit when the read is *not* ``True``.
        Useful when a handler logs the negative signal (e.g.
        ``"review.has_issues"``).

    Missing phases / fields and **non-bool values** are treated as falsy
    ("no opinion"). The strict-bool contract is deliberate: a verdict
    field semantically carries an approve/reject signal, and a handler
    that writes ``"false"`` (string), ``0``, or any other non-bool value
    has not declared a clean opinion. Coercing such values via
    :func:`bool` would let ``"false"`` silently exit a plan loop as if
    approved — the same hole :func:`pipeline.runtime.handoff.build_phase_handoff_signal`
    closes at the trigger side. The two consumers of the same field
    must agree.

    Asymmetry note (revisit if non-``approved``-style negated predicates
    return to built-in profiles): a ``"not <phase>.<field>"`` form is
    satisfied by missing or non-bool values because ``value`` reads
    falsy and the negation flips it to truthy ("loop exits as if the
    negative signal is absent"). For ``"not review.has_issues"`` that's
    the wrong default — a malformed ``has_issues`` value would silently
    short-circuit the review-changes loop. The built-in profiles in
    scope for slice 1 use ``validate_plan.approved`` /
    ``review_changes.clean`` (positive forms only), so the asymmetry is
    inert here; the right time to decide between "non-bool = falsy" and
    "non-bool = unknown, never satisfies either polarity" is when a
    negated predicate comes back into a built-in profile.
    """
    p = predicate.strip()
    negate = False
    if p.lower().startswith("not "):
        negate = True
        p = p[4:].strip()
    if "." not in p:
        raise ValueError(
            f"until predicate {predicate!r} must be 'phase.field' "
            f"(optionally prefixed with 'not')"
        )
    phase_name, _, field = p.partition(".")
    log_entry = state.phase_log.get(phase_name.strip())
    if isinstance(log_entry, dict):
        raw = log_entry.get(field.strip())
    elif isinstance(log_entry, list) and log_entry and isinstance(log_entry[-1], dict):
        # Some session-shape adapters store per-attempt entries as a list;
        # the last item is the most recent round's record.
        raw = log_entry[-1].get(field.strip())
    else:
        raw = None
    # Strict-bool reading: only ``True`` is truthy. ``isinstance(x, bool)``
    # filters out ``"false"`` and other non-bool truthy values that would
    # otherwise coerce to True under :func:`bool`.
    value = isinstance(raw, bool) and raw
    return (not value) if negate else value


__all__ = ["run_profile"]
