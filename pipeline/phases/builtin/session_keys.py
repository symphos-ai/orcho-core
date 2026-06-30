# SPDX-License-Identifier: Apache-2.0
"""Physical session addressing and resume policy for builtin handlers.

Computes the :class:`PhysicalSessionKey` for an invocation, resolves the
prompt-session split from the active step's execution policy, decides
``continue_session`` (the cross-phase resume probe and the loop-round
resume rule), maps an agent instance back to its ``PhaseAgentConfig``
slot, and exposes a serialisable dict view of a session key for trace
metadata. Pure leaf module: heavy prompt-session imports stay lazy and it
never imports back into ``pipeline.phases.builtin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pipeline.runtime.roles import SessionContinuity, SessionInvocationRole
from pipeline.runtime.run_shape import OperatingMode, operating_mode_from_state
from pipeline.runtime.session_disposition import SessionDisposition, decide

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


# ADR 0113 (declarative continuity): the *auxiliary* invocation roles whose
# continuity is a fixed property of the invocation shape, not a per-phase
# profile knob. They are always fresh: a companion / contract re-emit / audit
# invocation embeds whatever prior context it needs in its own prompt, so it
# must never drag a foreign provider transcript across. One documented
# constant, not a per-role table.
_AUXILIARY_CONTINUITY_ROLES: frozenset[SessionInvocationRole] = frozenset({
    SessionInvocationRole.COMPANION,
    SessionInvocationRole.FORMAT_REPAIR,
    SessionInvocationRole.AUDIT,
    SessionInvocationRole.VERIFICATION,
    SessionInvocationRole.BOUNDARY,
})

# The *phase* invocation roles whose continuity is declared per-step on the
# profile (``execution.session_continuity``). The resolver reads the active
# step's declaration for these; an undeclared phase fails loudly rather than
# silently defaulting to fresh.
_PHASE_CONTINUITY_ROLES: frozenset[SessionInvocationRole] = frozenset({
    SessionInvocationRole.PLAN,
    SessionInvocationRole.VALIDATE_PLAN,
    SessionInvocationRole.IMPLEMENT,
    SessionInvocationRole.REPAIR,
    SessionInvocationRole.REVIEW,
})


def _runtime_session_meta(
    agent: Any, *, continue_session: bool
) -> dict[str, Any]:
    """Policy-reflection session metadata for builtin handlers.

    Mirrors :func:`pipeline.phases.adapters._runtime_session_meta`: the
    ``continue_session`` recorded here is the *policy disposition* the handler
    actually passed to its invoke (the result of
    :func:`pipeline.runtime.session_disposition.decide` at the call site),
    reflected verbatim — never re-derived from
    ``agent._last_resumed_session_id``. That attribute only reports whether the
    runtime resume primitive happened to fire; a same-write-zone repair can
    carry a policy ``continue_session=True`` even when the provider session
    burned and the runtime no-ops the resume. The policy intent is the
    truthful value for this metadata, so this seam reflects it rather than
    becoming a second, independent continuity-deciding site.
    """
    meta: dict[str, Any] = {
        "session_id": getattr(agent, "session_id", None),
        "continue_session": continue_session,
    }
    parent_session_id = getattr(agent, "_last_followup_parent_session_id", None)
    if parent_session_id is not None:
        meta["followup_parent_session_id"] = parent_session_id
    return meta


def _operating_mode_for_state(state: PipelineState) -> OperatingMode:
    """Resolve the run's :class:`OperatingMode` for the disposition / sanction policy.

    Thin alias over the single posture reader
    :func:`pipeline.runtime.run_shape.operating_mode_from_state`: it reads the
    one ``state.extras['operating_mode']`` posture that
    :func:`pipeline.project.state_setup.build_pipeline_state` projects ONCE from
    the resolved verification work-mode / auto-detect ``actual_mode`` / profile
    default, falling back to the conservative ``FAST`` posture when unresolved.
    Kept as a named seam for the session-disposition + final_acceptance call
    sites, but never a second, independent resolver — the participant-promotion
    governed route reads the SAME source so the two can never diverge. Pure
    read — no I/O.
    """
    return operating_mode_from_state(state)


def _same_write_zone_for(
    state: PipelineState, *, role: SessionInvocationRole, phase: str
) -> bool:
    """Derive the *same-write-zone* policy input from run state.

    An edit-shaped follow-on (``implement`` / ``repair``) shares the prior
    write zone only when the run keeps a single physical worktree for the
    chained writers. CHAIN repair reuses the implement provider session and
    its worktree, so the repair handler records that CHAIN posture on
    ``state.extras['_repair_same_write_zone']`` — a dedicated same-write-zone
    signal, distinct from any continuity decision — and this seam reads it for
    the repair follow-on. Non-edit-shaped roles never continue, so the value is
    irrelevant for them and this stays a conservative read.

    ``phase`` is accepted for the seam's explicit (role, phase) contract; the
    implement-subtask follow-on derivation is wired by a later subtask.
    """
    del phase  # reserved for the implement-subtask follow-on derivation (T3)
    if role not in (SessionInvocationRole.IMPLEMENT, SessionInvocationRole.REPAIR):
        return False
    return (
        bool(state.extras.get("_repair_same_write_zone"))
        if state.extras else False
    )


def auxiliary_session_continuity(
    role: SessionInvocationRole,
) -> SessionContinuity:
    """Continuity for an *auxiliary* invocation role, resolved without state.

    The single source for the auxiliary-role continuity classification, shared
    by :func:`resolve_session_continuity` (the state-aware seam) and the
    state-less recovery call sites that have no ``PipelineState`` / active step
    to route through the seam — notably the ``format_repair`` contract re-emit
    in :mod:`pipeline.phases.review_contract_recovery`. Auxiliary roles are
    fresh by their invocation shape (they embed whatever prior context they
    need in their own prompt), so the classification needs no profile step and
    lives in exactly one place — never a second hard-coded ``FRESH_ONLY`` at a
    call site.

    A non-auxiliary (phase) role raises: those carry a per-phase declaration
    and must be resolved through :func:`resolve_session_continuity` so the
    declared continuity is honoured.
    """
    if role in _AUXILIARY_CONTINUITY_ROLES:
        return SessionContinuity.FRESH_ONLY
    raise ValueError(
        f"auxiliary_session_continuity: {role!r} is not an auxiliary role; "
        "phase roles carry a per-phase profile declaration and must be "
        "resolved via resolve_session_continuity()."
    )


def resolve_session_continuity(
    state: PipelineState, *, role: SessionInvocationRole, phase: str
) -> SessionContinuity:
    """Resolve the declarative continuity policy for an invocation.

    The single place a :class:`SessionInvocationRole` is mapped onto a
    :class:`SessionContinuity` member, mirroring
    :func:`_resolve_session_split_for_step` for the orthogonal split axis:

    * **Auxiliary roles** (``companion`` / ``format_repair`` / ``audit`` /
      ``verification`` / ``boundary``) → :attr:`SessionContinuity.FRESH_ONLY`.
      Their freshness is a property of the invocation shape, not a per-phase
      profile knob, so it is one documented constant rather than a table.
    * **Phase roles** (``plan`` / ``validate_plan`` / ``implement`` /
      ``repair`` / ``review``) → read ``session_continuity`` off the active
      step's :class:`ExecutionPolicy` (via ``lifecycle_ctx.active_step``, as
      :func:`_resolve_session_split_for_step` reads ``session_split``). The
      field is required for these: an active step with no declared
      ``session_continuity`` raises rather than silently defaulting to fresh —
      the import-time completeness guard's spirit, so a phase cannot regress to
      fresh continuity invisibly. A phase role with *no active step at all*
      also raises rather than guessing fresh: the FSM always seeds an active
      step for a real phase invocation, so a missing one means a broken wiring
      or an FSM bypass, and silently returning fresh there would let the
      plan/validate continuity regression creep back unnoticed on round 2+.
      Cross / standalone callers that legitimately have no profile step (the
      cross planner, the standalone DAG runner) do not route through this seam
      at all — they pass the policy explicitly to
      :func:`pipeline.runtime.session_disposition.decide`.

    A role in neither set raises — the classification stays explicit and
    exhaustive over :class:`SessionInvocationRole`.
    """
    if role in _AUXILIARY_CONTINUITY_ROLES:
        return auxiliary_session_continuity(role)
    if role in _PHASE_CONTINUITY_ROLES:
        ctx = getattr(state, "lifecycle_ctx", None)
        active_step = (
            getattr(ctx, "active_step", None) if ctx is not None else None
        )
        if active_step is None:
            # A phase role must arrive with an FSM-seeded active step carrying
            # its declared continuity. Missing one is not a legitimate "no
            # declaration" case (those paths — cross planner, standalone DAG
            # runner — pass policy explicitly to decide() and never hit this
            # seam); it means broken wiring or an FSM bypass. Refuse to guess
            # fresh, which would silently re-introduce the ADR 0113
            # plan/validate continuity regression on round 2+.
            raise ValueError(
                f"Phase {phase!r} (role {role.value!r}): no active step on "
                "lifecycle_ctx to read session_continuity from. A phase-role "
                "invocation must carry an FSM-seeded active step; standalone / "
                "cross callers with no profile step must pass the continuity "
                "policy explicitly to session_disposition.decide() instead of "
                "routing through this resolver — refusing to silently default "
                "to a fresh session."
            )
        policy = getattr(active_step, "execution_policy", None)
        declared = (
            getattr(policy, "session_continuity", None)
            if policy is not None else None
        )
        if declared is None:
            raise ValueError(
                f"Phase {phase!r} (role {role.value!r}): no session_continuity "
                "declared on the active step's execution policy. Declare it in "
                "the profile's execution block as one of "
                "fresh_only|loop_continue|same_zone_continue — refusing to "
                "silently default to a fresh session, which would re-introduce "
                "the ADR 0113 plan/validate continuity regression."
            )
        return SessionContinuity(declared)
    raise ValueError(
        "resolve_session_continuity: unclassified SessionInvocationRole "
        f"{role!r}; classify it as auxiliary or phase-declared."
    )


def _loop_round_followon(state: PipelineState, *, round_key: str) -> bool:
    """Whether this invocation is a loop follow-on (round 2+).

    Reads the handler-specific round counter from ``state.extras`` with a
    ``loop_round`` fallback for v2 ``LoopStep`` defaults — the same round-gating
    :func:`_should_resume` applies. Round 1 is fresh (no prior loop session yet);
    round 2+ is a follow-on the ``loop_continue`` policy resumes.
    """
    round_n = int(
        state.extras.get(round_key)
        or state.extras.get("loop_round")
        or 1
    )
    return round_n > 1


def decide_session_continuation(
    state: PipelineState,
    *,
    role: SessionInvocationRole,
    phase: str,
    round_key: str = "loop_round",
) -> SessionDisposition:
    """Role-explicit session-disposition seam.

    The single decision seam phases/adapters read with an *explicit*
    :class:`SessionInvocationRole`. It resolves the declarative continuity
    policy (:func:`resolve_session_continuity`) and derives the policy inputs
    (``same_write_zone`` from run state, ``loop_followon`` from the round
    counter, ``operating_mode``), then delegates the continue/fresh decision to
    :func:`pipeline.runtime.session_disposition.decide` — the one continuity
    decision site. The role is never inferred from the call stack or the
    caller's name; callers pass it. ``round_key`` selects the loop counter the
    ``loop_continue`` policy consults (``plan_round`` for the plan/validate
    loops, ``loop_round`` by default).
    """
    return decide(
        policy=resolve_session_continuity(state, role=role, phase=phase),
        same_write_zone=_same_write_zone_for(state, role=role, phase=phase),
        loop_followon=_loop_round_followon(state, round_key=round_key),
        operating_mode=_operating_mode_for_state(state),
    )


def _should_resume(
    state: PipelineState, *, role: SessionInvocationRole, round_key: str
) -> bool:
    """Legacy loop-round resume for the plan / validate_plan / review loops.

    The signature now carries an *explicit* :class:`SessionInvocationRole`,
    removing the old ``(state, round_key)`` ambiguity: ``review`` and ``repair``
    invocations both key ``"repair_round"`` yet need different dispositions, and
    the bare round key could not tell them apart. The role is the handle the
    loop handlers (plan / validate_plan / review) are switched onto
    :func:`decide_session_continuation` + a compact handoff by a later subtask;
    until that migration this body stays round-gated to preserve the shipped
    behaviour (round 2+ resumes) and avoid an amnesia regression (fresh without
    a handoff). Continuity for those handlers is therefore still round-derived
    here, not policy-derived — that retirement is the loop-handler subtask, not
    this one.

    Bridge resume is a runtime primitive: same runtime instance, prior
    session_id. Round 1 starts fresh (no captured bridge yet); round 2+ passes
    ``continue_session=True`` so the runtime sends ``--resume <id>``.

    Reads the handler-specific round counter from ``state.extras`` with a
    ``loop_round`` fallback for v2 LoopStep defaults.

    ADR 0039 extension: the post-repair re-verify pass inside the review/repair
    loop also wants the prior conversational context — the reviewer is auditing
    fixes to its own findings, so dropping that history forces a cold re-read of
    the diff. The loop runner sets ``state.extras["_review_reverify_resume"] =
    True`` just before the re-dispatch and clears it after, scoped to the
    ``repair_round`` key so other loops are unaffected.
    """
    if not isinstance(role, SessionInvocationRole):
        raise TypeError(
            "_should_resume: role must be a SessionInvocationRole, "
            f"got {type(role).__name__}"
        )
    if (
        round_key == "repair_round"
        and state.extras.get("_review_reverify_resume")
    ):
        return True
    round_n = int(
        state.extras.get(round_key)
        or state.extras.get("loop_round")
        or 1
    )
    return round_n > 1


def _resolve_session_split_for_step(state: PipelineState) -> Any:
    """ADR 0027 / M11: resolve the prompt-session split from the
    active step's :class:`ExecutionPolicy`, with a conservative
    per-phase default.

    The active step (set by the FSM in
    :class:`pipeline.lifecycle.PhaseLifecycle.execute_step`) carries
    an ``execution_policy`` whose ``session_split`` may be ``None``
    (no profile-level preference) or one of the four
    :class:`PromptSessionSplit` member strings. Returning ``None``
    is reserved for "no policy"; the helper resolves a conservative
    ``per_phase`` default in that case so legacy profiles keep the
    M7/M8/M9 behaviour they shipped with.
    """
    from pipeline.prompts.session import PromptSessionSplit

    ctx = getattr(state, "lifecycle_ctx", None)
    active_step = getattr(ctx, "active_step", None) if ctx is not None else None
    policy = getattr(active_step, "execution_policy", None)
    policy_split = getattr(policy, "session_split", None) if policy else None
    if policy_split is None:
        return PromptSessionSplit.PER_PHASE
    try:
        return PromptSessionSplit(policy_split)
    except ValueError:
        # ExecutionPolicy.__post_init__ already validated the value
        # domain at load time, so this branch is defensive only.
        return PromptSessionSplit.PER_PHASE


def _compute_session_key(
    state: PipelineState,
    agent: Any,
    *,
    phase: str,
    split: Any,
) -> Any:
    """Compute the :class:`PhysicalSessionKey` for an invocation.

    Single source of truth shared by :func:`_session_aware_invoke` and
    :func:`_should_continue_prompt_session` so the resume probe and the
    render path can never key the same call differently. Runtime is
    anchored on the agent class (two instances of the same class share a
    key); model on ``agent.model``; ``run_id`` isolates state per run.

    ADR 0027 / M11: PER_ROLE keys require an explicit
    ``active_step.prompt.role``; absent one we raise rather than
    silently degrade to a per_phase-shaped key under a different label.
    """
    from pipeline.prompts.session import PromptSessionSplit, make_session_key

    runtime_id = f"{type(agent).__module__}.{type(agent).__qualname__}"
    model_key = str(getattr(agent, "model", "") or "")
    run_id = str(state.extras.get("run_id", "") or "unknown")
    role_for_key: str | None = None
    if split is PromptSessionSplit.PER_ROLE:
        ctx = getattr(state, "lifecycle_ctx", None)
        active_step = (
            getattr(ctx, "active_step", None) if ctx is not None else None
        )
        prompt_spec = getattr(active_step, "prompt", None)
        role_for_key = getattr(prompt_spec, "role", None)
        if not role_for_key:
            raise ValueError(
                f"Phase {phase!r}: session_split='per_role' requires "
                "active_step.prompt.role to be set on the same step; "
                "without an explicit role the per_role split would "
                "silently degrade to per_phase under a different label"
            )
    return make_session_key(
        run_id=run_id,
        runtime=runtime_id,
        model_key=model_key,
        split=split,
        phase=phase,
        role=role_for_key,
    )


def _should_continue_prompt_session(
    state: PipelineState,
    agent: Any,
    *,
    phase: str,
    role: SessionInvocationRole,
    split: Any = None,
) -> bool:
    """Agent-keyed same-write-zone continuation seam (policy-backed).

    This is the continuation seam for the stored-session (implement /
    implement-subtask) call sites, where the same-write-zone signal can only be
    derived from the *agent* (the physical :class:`PhysicalSessionKey` it would
    resume), not from ``state`` alone — hence a distinct seam from
    :func:`decide_session_continuation`. Both seams route the final
    continue/fresh choice through the one policy site
    (:func:`pipeline.runtime.session_disposition.decide`) with an **explicit**
    :class:`SessionInvocationRole`; the role is never inferred from the call
    stack.

    The **same-write-zone rule**: an edit-shaped follow-on shares the prior
    write zone when a reusable :class:`PhysicalSessionKey` already carries a
    committed provider ``session_id`` for this invocation — i.e. a same-run,
    same-worktree predecessor (an earlier ``common``/shared ``per_role`` phase,
    or a prior same-agent subtask) seeded it. That is also exactly the
    condition under which the M6 delta selector may omit already-sent stable
    parts, so the session it would resume physically exists. This function
    derives that ``same_write_zone`` signal and hands it, with the explicit
    role, to the policy:

    * ``implement`` (edit-shaped) + same-write-zone → CONTINUE (resume the
      seeded session); no reusable session (fresh agent / new zone / STATELESS
      split) → FRESH.
    * a non-edit-shaped role (e.g. ``companion``) → FRESH regardless, so its
      compact handoff rides a fresh wire.

    ``_session_aware_invoke`` then seeds the (distinct) phase agent instance
    from the stored session id so a CONTINUE resume is physically real.
    """
    from pipeline.prompts.session import PromptSessionSplit

    if split is None:
        split = _resolve_session_split_for_step(state)
    same_write_zone = False
    if split is not PromptSessionSplit.STATELESS:
        key = _compute_session_key(state, agent, phase=phase, split=split)
        if key is not None:
            stored = state.prompt_sessions.get(key)
            same_write_zone = bool(stored is not None and stored.session_id)
    return decide(
        policy=resolve_session_continuity(state, role=role, phase=phase),
        same_write_zone=same_write_zone,
        loop_followon=False,
        operating_mode=_operating_mode_for_state(state),
    ).continue_session


def _physical_session_key_to_dict(key: Any) -> dict[str, str] | None:
    """Stable dict view of a :class:`PhysicalSessionKey` for trace metadata.

    M12 trace persistence reads this; M7 emits it. Returning a dict
    rather than a frozen dataclass keeps the trace surface
    serialisation-friendly without forcing a dependency on the
    session module at the persistence layer.
    """
    if key is None:
        return None
    return {
        "run_id": key.run_id,
        "runtime": key.runtime,
        "model_key": key.model_key,
        "scope": key.scope,
    }


# E1: role_attr slots used to persist + rehydrate ``agent.session_id``
# in the checkpoint. Mirrors
# ``pipeline.project_orchestrator._FOLLOWUP_ROLE_TO_AGENT_ATTR`` values;
# kept local here so this module does not import from
# ``project_orchestrator`` (would close a cycle).
_AGENT_ROLE_ATTRS: tuple[str, ...] = (
    "plan_agent",
    "validate_plan_agent",
    "implement_agent",
    "review_changes_agent",
    "repair_changes_agent",
    "final_acceptance_agent",
)


def _agent_to_role_attr(state: PipelineState, agent: Any) -> str | None:
    """Return the ``PhaseAgentConfig`` slot ``agent`` is bound to.

    Identity-keyed (``is`` comparison) — works correctly for CHAIN-mode
    dispatch where ``phase="repair_changes"`` invokes the same instance
    as ``implement_agent``. ``None`` when ``phase_config`` is unset or
    the agent isn't a known role (e.g. ad-hoc agents in tests).
    """
    pc = state.phase_config
    if pc is None:
        return None
    for attr in _AGENT_ROLE_ATTRS:
        if getattr(pc, attr, None) is agent:
            return attr
    return None
