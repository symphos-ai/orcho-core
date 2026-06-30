"""pipeline/cross_project/profile_projection.py — project a v2 ``Profile``
into cross-level and per-project step lists for ``orcho cross``.

A profile may carry per-step ``cross`` policy (``CrossScope`` + optional
handler). Projection splits the profile into:

  ``global_steps``  — steps that run once at the cross level (shared
                       plan, validate_plan, etc.).
  ``project_steps`` — steps that run inside every child project's
                       sub-pipeline (implement, review_changes, …).

Rules:
  * ``scope=global``  → only in ``global_steps``.
  * ``scope=project`` → only in ``project_steps``.
  * ``scope=both``    → both lists (rare).
  * ``scope=skip``    → omitted.
  * ``LoopStep`` is projected wholesale: all inner ``PhaseStep`` policies
    must agree (``all global`` / ``all project`` / ``all skip``); mixed
    loops are rejected.
  * Missing ``cross`` on any ``PhaseStep`` → error in cross mode.
  * Coherence: if any projected project step is ``implement`` or
    ``repair_changes`` but the projection has no global ``plan`` /
    ``validate_plan`` step (no handoff source), the profile is rejected.

``CrossStepPolicy.handler`` is dispatch metadata only — ``PhaseStep.phase``
is preserved verbatim so loop predicates like
``until: validate_plan.approved`` continue to evaluate against the
semantic phase name.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline.runtime import (
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
    CrossScope,
    LoopStep,
    PhaseStep,
    Profile,
)

__all__ = [
    "CrossProjection",
    "CrossProjectionError",
    "KNOWN_CROSS_GATES",
    "get_cross_gate_policy",
    "project_cross_profile",
]


# Phases that require an approved cross handoff before they may run inside
# a child project. ``final_acceptance`` may consume the handoff when present
# but does not require it (it inspects diffs, not plans).
_HANDOFF_CONSUMERS = frozenset({"implement", "repair_changes"})
# Phases that produce a handoff at the cross level. If any handoff-consumer
# project step is present in projection, at least one of these must appear
# as a global step.
_HANDOFF_PRODUCERS = frozenset({"plan", "validate_plan"})

# Known cross-level handler names that may appear on a profile step. The
# cross runner dispatches a global step by ``step.cross.handler`` rather
# than ``step.phase`` so the semantic phase name is preserved for loop
# predicates. Projection validates the handler exists; unknown handlers
# fail fast at projection time, not deep inside the cross dispatcher.
#
# ``contract_check`` is intentionally NOT here: it is a cross-only
# terminal gate the runner invokes after project pipelines finish. It
# must not appear in any profile's ``steps`` (mono runs would otherwise
# inherit it, which would break the "cross policy must not change mono
# semantics" invariant).
KNOWN_CROSS_HANDLERS = frozenset({
    "cross_plan",
    "cross_validate_plan",
})
# Phase names that the cross runner reserves for its own use. Declaring
# any of these in a profile is a profile-authoring bug:
#   * ``contract_check`` — cross-only per-alias terminal gate. Mono
#     runs would pick it up as an unknown phase; cross runs would
#     attempt to invoke it twice.
#   * ``cross_final_acceptance`` — cross-only system release gate
#     (ADR 0025 Phase 3). Same reservation logic: invoked by the cross
#     runner after contract_check, not via projection.
_RESERVED_PHASE_NAMES = frozenset({"contract_check", "cross_final_acceptance"})

# Runner-owned cross gates that may carry a ``cross_gates`` policy block
# on a profile. Profile loading rejects any other key; consumption-time
# defaults below describe what happens when a known gate has no explicit
# entry on the profile.
KNOWN_CROSS_GATES = frozenset({"contract_check", "cross_final_acceptance"})

# Consumption-time fallback. A profile that does NOT carry a
# ``cross_gates`` entry for a given gate runs that gate as DISABLED.
# Missing ≡ off. Profile authors opt INTO cross gates explicitly; the
# runtime never imposes them on profiles that don't ask.
#
# Each per-gate value is a frozen ``CrossGatePolicy`` with
# ``enabled=False``. The runner short-circuits disabled gates upstream
# and writes a ``policy_disabled`` skipped audit entry — same path
# whether the gate was explicitly disabled or absent.
_DISABLED_CROSS_GATE_POLICIES: dict[str, CrossGatePolicy] = {
    "contract_check": CrossGatePolicy(
        enabled=False,
        run=CrossGateRunPolicy.AUTO,
        on_skip=CrossGateSkipPolicy.BLOCK,
        mode=None,
    ),
    "cross_final_acceptance": CrossGatePolicy(
        enabled=False,
        run=CrossGateRunPolicy.AUTO,
        on_skip=CrossGateSkipPolicy.BLOCK,
        mode=None,
    ),
}


def get_cross_gate_policy(profile: Profile, gate_name: str) -> CrossGatePolicy:
    """Resolve the effective ``CrossGatePolicy`` for one runner-owned cross
    gate on ``profile``.

    Returns the profile's explicit entry when present, otherwise a
    disabled fallback for that gate — missing ≡ off. Raises
    ``KeyError`` for unknown gates so callers don't silently mask
    typos (the only legitimate inputs are ``KNOWN_CROSS_GATES``).
    """
    if gate_name not in KNOWN_CROSS_GATES:
        raise KeyError(
            f"unknown cross gate {gate_name!r}; "
            f"known: {sorted(KNOWN_CROSS_GATES)}"
        )
    explicit = profile.cross_gates.get(gate_name)
    if explicit is not None:
        return explicit
    return _DISABLED_CROSS_GATE_POLICIES[gate_name]


class CrossProjectionError(ValueError):
    """Raised when a profile cannot be projected for cross mode."""


@dataclass(frozen=True)
class CrossProjection:
    """Result of projecting a ``Profile`` for ``orcho cross``.

    ``global_steps`` runs through the cross step dispatcher once per
    cross-run. ``project_steps`` is wrapped into a synthetic in-memory
    ``Profile`` and runs inside each child project's ``run_pipeline``.
    """
    profile_name: str
    global_steps: tuple
    project_steps: tuple


def _scope_for(step: PhaseStep, ctx: str) -> CrossScope:
    if step.cross is None:
        raise CrossProjectionError(
            f"{ctx}: step {step.phase!r} has no cross policy.\n"
            f"Add cross.scope = 'global', 'project', 'both', or 'skip'."
        )
    return step.cross.scope


def _validate_global_handler(step: PhaseStep, ctx: str) -> None:
    """Global steps must declare a recognised handler.

    The cross runner dispatches by ``step.cross.handler`` (not by
    ``step.phase``) so the semantic phase name stays intact for loop
    predicates. A missing or unknown handler is a profile authoring bug
    and must fail at projection time, not at dispatch time.
    """
    if step.cross is None or step.cross.handler is None:
        raise CrossProjectionError(
            f"{ctx}: global step {step.phase!r} must declare "
            f"cross.handler. Known handlers: "
            f"{sorted(KNOWN_CROSS_HANDLERS)}."
        )
    if step.cross.handler not in KNOWN_CROSS_HANDLERS:
        raise CrossProjectionError(
            f"{ctx}: global step {step.phase!r} declares unknown "
            f"cross.handler={step.cross.handler!r}. Known handlers: "
            f"{sorted(KNOWN_CROSS_HANDLERS)}."
        )


def _project_loop(loop: LoopStep, profile_name: str, idx: int):
    """Return ``(maybe_global_loop, maybe_project_loop)``.

    A loop projects to at most one side: all inner steps must share scope.
    Mixed scopes raise ``CrossProjectionError`` — splitting a loop across
    levels would require duplicating predicate state, which the cross
    runner does not model.
    """
    ctx = f"profile {profile_name!r} step[{idx}]"
    scopes = {_scope_for(s, f"{ctx}.loop") for s in loop.steps}
    if CrossScope.BOTH in scopes:
        raise CrossProjectionError(
            f"{ctx}: cross.scope='both' is not supported inside a loop. "
            f"Move the step out of the loop or use a single-level scope."
        )
    distinct = scopes - {CrossScope.SKIP}
    if len(distinct) > 1:
        raise CrossProjectionError(
            f"{ctx}: mixed cross scopes inside loop "
            f"({sorted(s.value for s in distinct)}); a LoopStep must "
            f"project to a single level."
        )
    if not distinct:
        return None, None  # entire loop skipped
    only = next(iter(distinct))
    if CrossScope.SKIP in scopes:
        raise CrossProjectionError(
            f"{ctx}: cannot mix scope='skip' with another scope inside a loop."
        )
    if only is CrossScope.GLOBAL:
        # Every inner step is dispatched at the cross level; each one must
        # declare a recognised handler.
        for inner_idx, inner in enumerate(loop.steps):
            _validate_global_handler(inner, f"{ctx}.loop.steps[{inner_idx}]")
        return loop, None
    return None, loop


def _downgrade_cross_unsupported_implement_handoff(entry: PhaseStep) -> PhaseStep:
    """Bypass the implement substance-repair handoff for cross projection.

    ADR 0073 adds a per-step ``handoff`` policy on the top-level ``implement``
    phase so a mono (single-project) ``subtask_dag`` run pauses on an INCOMPLETE
    delivery instead of hard-stopping. The cross orchestrator does NOT yet honour
    an implement-phase pause inside a child project (an ADR 0038 / Part-B
    follow-up — it would need cross surfacing + a cross E2E mock smoke), and
    ``continue_with_waiver`` is already deliberately single-project only.

    To keep the mono-default ``feature`` profile cross-projectable without
    silently introducing an unhonoured cross pause, projecting it for cross
    downgrades a non-bypass ``implement`` handoff to bypass. Cross runs therefore
    keep their pre-ADR-0073 behaviour exactly: an incomplete ``subtask_dag``
    delivery hard-stops the child project. Mono runs are unaffected.
    """
    import dataclasses

    from pipeline.runtime.roles import PhaseHandoffType

    policy = entry.handoff
    if (
        entry.phase == "implement"
        and policy is not None
        and policy.type is not PhaseHandoffType.HUMAN_BYPASS
    ):
        return dataclasses.replace(entry, handoff=None)
    return entry


def project_cross_profile(profile: Profile) -> CrossProjection:
    """Split ``profile.steps`` into global / project lists for cross mode.

    Raises ``CrossProjectionError`` with an actionable message if any
    step lacks cross policy, if a loop mixes scopes, or if the projection
    violates the handoff coherence rule.

    Phase-handoff slice 1: cross-project does not yet honour
    ``PhaseStep.handoff``; the cross orchestrator is the next slice. To
    avoid a silent unsupported mode (declared policy that neither
    pauses nor fails fast), projection rejects any non-bypass handoff
    that ends up in the **projected output** — i.e. on a PhaseStep
    whose ``cross.scope`` lands it in ``global_steps`` or
    ``project_steps``. A step explicitly marked ``cross.scope="skip"``
    is dropped from the projection and therefore never reaches the
    cross runner, so its handoff is irrelevant and does not trigger
    the guard.
    """
    _reject_reserved_phases(profile)
    global_steps: list = []
    project_steps: list = []
    for i, entry in enumerate(profile.steps):
        if isinstance(entry, LoopStep):
            g, p = _project_loop(entry, profile.name, i)
            if g is not None:
                global_steps.append(g)
            if p is not None:
                project_steps.append(p)
            continue
        if not isinstance(entry, PhaseStep):
            raise CrossProjectionError(
                f"profile {profile.name!r} step[{i}]: unsupported entry type "
                f"{type(entry).__name__}"
            )
        ctx = f"profile {profile.name!r} step[{i}]"
        scope = _scope_for(entry, ctx)
        if scope in (CrossScope.GLOBAL, CrossScope.BOTH):
            _validate_global_handler(entry, ctx)
        if scope is CrossScope.GLOBAL:
            global_steps.append(entry)
        elif scope is CrossScope.PROJECT:
            project_steps.append(
                _downgrade_cross_unsupported_implement_handoff(entry)
            )
        elif scope is CrossScope.BOTH:
            global_steps.append(entry)
            project_steps.append(
                _downgrade_cross_unsupported_implement_handoff(entry)
            )
        # SKIP → drop

    # Phase 5+6 handoff guard runs on the projected entries, not the raw
    # profile — see ``_reject_non_bypass_handoff`` for the contract.
    _reject_non_bypass_handoff_in_projection(
        profile.name, global_steps, project_steps,
    )

    _enforce_coherence(profile.name, global_steps, project_steps)

    return CrossProjection(
        profile_name=profile.name,
        global_steps=tuple(global_steps),
        project_steps=tuple(project_steps),
    )


#: Cross handlers that honour the single-run phase-handoff lifecycle at
#: the cross level. Project-scoped ``review_changes`` is handled inside
#: the child single-project runner and proxied by the cross parent.
_CROSS_HANDOFF_SUPPORTED_HANDLERS: frozenset[str] = frozenset({
    "cross_validate_plan",
})

_CROSS_HANDOFF_SUPPORTED_PROJECT_PHASES: frozenset[str] = frozenset({
    "review_changes",
})


def _reject_non_bypass_handoff_in_projection(
    profile_name: str,
    global_steps: list,
    project_steps: list,
) -> None:
    """Refuse a projection whose entries declare non-bypass ``handoff``
    on a PhaseStep whose cross handler does not (yet) honour the
    single-run phase-handoff lifecycle.

    ADR 0038: ``cross_validate_plan`` now honours
    ``human_feedback_on_reject`` end-to-end (pause → operator decides
    via ``orcho_phase_handoff_decide`` → resume). Other cross-projected
    PhaseSteps still get the original silent-bypass guard — declaring
    non-bypass handoff there would let the cross run sail past
    rejections without the pause the single-project runner honours.

    The check walks the **projected output**, so a PhaseStep whose
    ``cross.scope="skip"`` (dropped from both lists during projection)
    is exempt — its handoff never reaches the cross runner.

    The legacy entry point :func:`_reject_non_bypass_handoff` is kept
    as a thin shim against ``profile.steps`` for tests that want to
    exercise the strict "raw profile" semantics directly; production
    dispatch uses this projection-scoped variant.
    """
    from pipeline.runtime.roles import PhaseHandoffType

    def _check_step(step: PhaseStep, where: str) -> None:
        policy = step.handoff
        if policy is None or policy.type is PhaseHandoffType.HUMAN_BYPASS:
            return
        cross_policy = step.cross
        handler = cross_policy.handler if cross_policy is not None else None
        if (
            handler in _CROSS_HANDOFF_SUPPORTED_HANDLERS
            and policy.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT
        ):
            # ADR 0038 supported combination — cross orchestrator honours
            # this exactly like single-run validate_plan.
            return
        if (
            step.phase in _CROSS_HANDOFF_SUPPORTED_PROJECT_PHASES
            and cross_policy is not None
            and cross_policy.scope is CrossScope.PROJECT
            and policy.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT
        ):
            return
        raise CrossProjectionError(
            f"profile {profile_name!r} {where}: PhaseStep {step.phase!r} "
            f"declares handoff type {policy.type.value!r} under cross "
            f"handler {handler!r}, but the cross orchestrator only honours "
            f"'human_feedback_on_reject' on cross handlers "
            f"{sorted(_CROSS_HANDOFF_SUPPORTED_HANDLERS)} or project "
            f"phases {sorted(_CROSS_HANDOFF_SUPPORTED_PROJECT_PHASES)} "
            "today. Either "
            "switch the step to 'human_bypass' for cross projection, "
            "select a profile without this combination (e.g. 'small_task'), or "
            "extend cross handoff support (ADR 0038 follow-up)."
        )

    def _walk(entries: list, side_label: str) -> None:
        for idx, entry in enumerate(entries):
            if isinstance(entry, PhaseStep):
                _check_step(entry, f"{side_label}[{idx}]")
            elif isinstance(entry, LoopStep):
                for inner_idx, inner in enumerate(entry.steps):
                    if isinstance(inner, PhaseStep):
                        _check_step(
                            inner,
                            f"{side_label}[{idx}].loop.steps[{inner_idx}]",
                        )

    _walk(global_steps, "global_steps")
    _walk(project_steps, "project_steps")


def _reject_non_bypass_handoff(profile: Profile) -> None:
    """Test/back-compat entry point — walks ``profile.steps`` directly
    and rejects any non-bypass handoff regardless of cross scope.

    Production dispatch goes through
    :func:`_reject_non_bypass_handoff_in_projection` after projection so
    ``cross.scope="skip"`` correctly exempts a step's handoff. This
    function stays here because the autouse conftest fixtures in
    ``tests/acceptance/`` and ``tests/unit/pipeline/cross_project/``
    monkey-patch this symbol to a no-op to keep legacy cross tests
    working against built-in profiles that now declare handoff.
    """
    from pipeline.runtime.roles import PhaseHandoffType

    def _check_step(step: PhaseStep, where: str) -> None:
        policy = step.handoff
        if policy is None or policy.type is PhaseHandoffType.HUMAN_BYPASS:
            return
        raise CrossProjectionError(
            f"profile {profile.name!r} {where}: PhaseStep {step.phase!r} "
            f"declares handoff type {policy.type.value!r}, but cross-project "
            "phase handoff lands in a later slice. Use 'human_bypass' on "
            "cross-projected profiles, switch to a profile without "
            "non-bypass handoff (e.g. 'small_task'), or wait for the cross "
            "handoff cutover."
        )

    for idx, entry in enumerate(profile.steps):
        if isinstance(entry, PhaseStep):
            _check_step(entry, f"step[{idx}]")
        elif isinstance(entry, LoopStep):
            for inner_idx, inner in enumerate(entry.steps):
                if isinstance(inner, PhaseStep):
                    _check_step(inner, f"step[{idx}].loop.steps[{inner_idx}]")


def _reject_reserved_phases(profile: Profile) -> None:
    """Fail projection if any step's ``phase`` is reserved for the runner.

    ``contract_check`` is the canonical reserved name: it is the cross-only
    terminal gate and must not be declared as a profile step. Mono runs
    would otherwise pick it up as an unknown phase, and cross runs would
    invoke it twice. Reject at projection time so authors see the error
    instantly, not deep inside the cross dispatcher.
    """
    for i, entry in enumerate(profile.steps):
        steps: tuple
        if isinstance(entry, LoopStep):
            steps = entry.steps
        elif isinstance(entry, PhaseStep):
            steps = (entry,)
        else:
            continue
        for inner in steps:
            if inner.phase in _RESERVED_PHASE_NAMES:
                raise CrossProjectionError(
                    f"profile {profile.name!r} step[{i}]: phase "
                    f"{inner.phase!r} is reserved for the cross runner. "
                    f"Remove it from the profile — the runner invokes "
                    f"contract_check and cross_final_acceptance "
                    f"automatically as terminal gates."
                )


def _phase_names_in(entries) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, LoopStep):
            names.update(s.phase for s in entry.steps)
        elif isinstance(entry, PhaseStep):
            names.add(entry.phase)
    return names


def _enforce_coherence(
    profile_name: str, global_steps: list, project_steps: list
) -> None:
    project_phases = _phase_names_in(project_steps)
    consumers = project_phases & _HANDOFF_CONSUMERS
    if not consumers:
        return
    global_phases = _phase_names_in(global_steps)
    if global_phases & _HANDOFF_PRODUCERS:
        return
    raise CrossProjectionError(
        f"profile {profile_name!r} cannot run in cross mode: it contains "
        f"project-scoped {sorted(consumers)} but no global planning step "
        f"({sorted(_HANDOFF_PRODUCERS)}) to produce a handoff. Use a "
        f"profile that includes a cross-level plan / validate_plan, or "
        f"add cross.scope='global' to its planning step."
    )
