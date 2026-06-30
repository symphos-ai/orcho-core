"""Delta-selection pure function for ADR-0026 session-aware rendering.

M6 decides which prompt parts to send on a resumed turn, given the
M2 :class:`PromptRenderEnvelope`, the M5 :class:`PromptSessionState`
recording what was sent before, and the M5
:class:`PromptSessionSplit` policy. The selector is purely
declarative — no I/O, no provider integration, no envelope or
state mutation. It returns a :class:`SelectionResult` telling the
caller which parts to render on the wire and what state update to
apply once the agent invocation succeeds.

M7 wires this into the validate-plan adapter (the first phase to
adopt session-aware rendering at runtime) and is responsible for
the "apply state_update only after a successful invocation" rule:
M6 returns a candidate update; the caller commits it.

The selector signature and :class:`SelectionResult` shape become
the frozen surface for M7+, M8/M9 wiring milestones, and M12 trace
persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.prompts.envelope import PromptRenderEnvelope
from pipeline.prompts.session import (
    PromptSessionSplit,
    PromptSessionState,
    part_session_key,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)


@dataclass(frozen=True)
class SelectionResult:
    """One delta-selection outcome.

    ``selected_parts`` are the parts the caller renders into the
    wire prompt for this turn; ``omitted_parts`` are the parts the
    selector decided are already cached on the runtime side and may
    be skipped. Both lists preserve render order.

    ``selected_part_keys`` and ``omitted_part_keys`` mirror the
    same lists in canonical :func:`part_session_key` form (``id +
    version``), so M12 trace persistence can record exactly which
    parts went over the wire without re-resolving.

    ``render_mode`` reflects the **rendering strategy**, not the
    presence of omissions: ``"full"`` means the selector did not
    attempt session reuse (stateless / no state / fresh session);
    ``"delta"`` means it did, regardless of whether anything was
    actually omitted on this turn.

    ``state_update`` is a candidate :class:`PromptSessionState` to
    apply **only after** a successful agent invocation. ``None``
    means "no reusable state for this call" — either stateless,
    or the caller has not yet created a state under a
    :class:`~pipeline.prompts.session.PhysicalSessionKey` (M7's
    job).

    ``delta_dropped_parts`` are parts the caller explicitly asked to
    omit from the wire on a *resumed delta* turn (via
    ``delta_droppable_part_ids``) because the runtime already holds
    them in conversation history — e.g. the original task on a replan
    round. They are NOT on the wire (excluded from ``selected_parts``)
    and add no new ``sent_part_keys`` this turn, but any prior key for
    them is preserved in ``state_update`` (cumulative session memory).
    Empty on full renders — drop only ever happens on the delta path.
    """

    selected_parts: tuple[PromptPart, ...]
    omitted_parts: tuple[PromptPart, ...]
    selected_part_keys: tuple[str, ...]
    omitted_part_keys: tuple[str, ...]
    render_mode: Literal["full", "delta"]
    state_update: PromptSessionState | None
    delta_dropped_parts: tuple[PromptPart, ...] = ()
    delta_dropped_part_keys: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Active-anchor helpers — derive the role / phase / contract identities
# the envelope's parts represent. Used by the delta path to detect
# context flips that force a contract / role / phase resend.
#
# Anchor ids are bare ``PromptPart.id`` (semantic identity), not
# ``part_session_key`` (id + version): a contract switch is a different
# semantic surface, while a version bump on the same surface is
# already handled by the version-aware send-history key.
# ---------------------------------------------------------------------------


def _envelope_role_id(envelope: PromptRenderEnvelope) -> str | None:
    for p in envelope.parts:
        if p.layer is PromptLayer.ROLE:
            return p.id
    return None


def _envelope_phase_id(envelope: PromptRenderEnvelope) -> str | None:
    for p in envelope.parts:
        if p.layer is PromptLayer.PHASE:
            return p.id
    return None


def _envelope_contract_ids(envelope: PromptRenderEnvelope) -> frozenset[str]:
    return frozenset(
        p.id for p in envelope.parts if p.layer is PromptLayer.CONTRACT
    )


# ---------------------------------------------------------------------------
# Always-resend rule. Volatile parts cannot be cached — every turn
# must include them regardless of whether they were "sent" before.
# ---------------------------------------------------------------------------


def _is_always_send(part: PromptPart) -> bool:
    return (
        part.cache_scope is PromptCacheScope.NONE
        or part.stability is PromptStability.TURN
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _build_result(
    *,
    selected: list[PromptPart],
    omitted: list[PromptPart],
    render_mode: Literal["full", "delta"],
    state_update: PromptSessionState | None,
    dropped: list[PromptPart] | None = None,
) -> SelectionResult:
    dropped = dropped or []
    return SelectionResult(
        selected_parts=tuple(selected),
        omitted_parts=tuple(omitted),
        selected_part_keys=tuple(part_session_key(p) for p in selected),
        omitted_part_keys=tuple(part_session_key(p) for p in omitted),
        render_mode=render_mode,
        state_update=state_update,
        delta_dropped_parts=tuple(dropped),
        delta_dropped_part_keys=tuple(part_session_key(p) for p in dropped),
    )


def _full_render_no_state_update(
    envelope: PromptRenderEnvelope,
) -> SelectionResult:
    return _build_result(
        selected=list(envelope.parts),
        omitted=[],
        render_mode="full",
        state_update=None,
    )


def _full_render_with_candidate_update(
    envelope: PromptRenderEnvelope,
    state: PromptSessionState,
) -> SelectionResult:
    selected = list(envelope.parts)
    candidate = PromptSessionState(
        key=state.key,
        session_id=state.session_id,
        sent_part_keys=state.sent_part_keys
        | {part_session_key(p) for p in selected},
        active_role_id=_envelope_role_id(envelope) or state.active_role_id,
        active_phase_id=_envelope_phase_id(envelope) or state.active_phase_id,
        active_contract_ids=_envelope_contract_ids(envelope)
        or state.active_contract_ids,
    )
    return _build_result(
        selected=selected,
        omitted=[],
        render_mode="full",
        state_update=candidate,
    )


def _delta_render(
    envelope: PromptRenderEnvelope,
    state: PromptSessionState,
    delta_droppable_part_ids: frozenset[str] = frozenset(),
) -> SelectionResult:
    new_role_id = _envelope_role_id(envelope)
    new_phase_id = _envelope_phase_id(envelope)
    new_contract_ids = _envelope_contract_ids(envelope)

    role_changed = (
        new_role_id is not None and new_role_id != state.active_role_id
    )
    phase_changed = (
        new_phase_id is not None and new_phase_id != state.active_phase_id
    )
    contracts_changed = (
        bool(new_contract_ids)
        and new_contract_ids != state.active_contract_ids
    )

    # M11.5: only contiguous-prefix parts are omit-safe. The M2
    # envelope partitioner already enforces contiguity at construction
    # time, but the selector kept its own scope check ad-hoc and would
    # otherwise drop a stable contract or role that the partitioner
    # had classified as payload (e.g. a prefix-eligible part rendered
    # after a turn part is partitioned into payload by
    # ``_split_at_first_payload``). Computing the omit-safe set from
    # ``envelope.stable_prefix_parts`` makes the invariant explicit
    # and survives any future loosening of the partition rule.
    omit_safe_ids = {id(p) for p in envelope.stable_prefix_parts}

    selected: list[PromptPart] = []
    omitted: list[PromptPart] = []
    for part in envelope.parts:
        # Volatile parts always go on the wire — cache_scope=NONE
        # and stability=TURN parts are not cacheable by definition.
        if _is_always_send(part):
            selected.append(part)
            continue
        # Unseen by version-aware send-history key.
        if part_session_key(part) not in state.sent_part_keys:
            selected.append(part)
            continue
        # Forced resend on context flip — the active semantic surface
        # changed, so the previously-sent part no longer matches what
        # the agent should be acting on.
        if role_changed and part.layer is PromptLayer.ROLE:
            selected.append(part)
            continue
        if phase_changed and part.layer is PromptLayer.PHASE:
            selected.append(part)
            continue
        if contracts_changed and part.layer is PromptLayer.CONTRACT:
            selected.append(part)
            continue
        # Prefix-contiguity defense: a stable already-sent part that
        # sits OUTSIDE the M2 contiguous prefix must still go on the
        # wire. Omitting it would re-stitch a payload-position part
        # the agent never saw at that wire location on round 1.
        if id(part) not in omit_safe_ids:
            selected.append(part)
            continue
        # Otherwise the part is already cached on the runtime side
        # and stable for the active context — safe to omit.
        omitted.append(part)

    # Caller-requested wire drop (resumed delta only): parts the caller
    # judges already represented in the runtime's conversation history
    # (e.g. the original task — sent on round 1, present in history even
    # when this round's part carries a different id like
    # ``turn_input:replan_task`` vs round 1's ``turn_input:plan_task``).
    # Partition them OUT of ``selected`` AFTER the selection loop and BEFORE
    # building ``candidate`` so this turn adds no new ``sent_part_keys`` for
    # them, while any prior key already in ``state.sent_part_keys`` stays
    # untouched (cumulative session memory; the union below only grows).
    # Caller owns the "already in history" judgement — the drop fires only on
    # the resumed-delta path, where round 1 established that history.
    dropped: list[PromptPart] = []
    if delta_droppable_part_ids:
        kept: list[PromptPart] = []
        for part in selected:
            if part.id in delta_droppable_part_ids:
                dropped.append(part)
            else:
                kept.append(part)
        selected = kept

    candidate = PromptSessionState(
        key=state.key,
        session_id=state.session_id,
        sent_part_keys=state.sent_part_keys
        | {part_session_key(p) for p in selected},
        active_role_id=new_role_id or state.active_role_id,
        active_phase_id=new_phase_id or state.active_phase_id,
        active_contract_ids=new_contract_ids or state.active_contract_ids,
    )
    return _build_result(
        selected=selected,
        omitted=omitted,
        render_mode="delta",
        state_update=candidate,
        dropped=dropped,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def will_render_delta(
    state: PromptSessionState | None,
    split: PromptSessionSplit,
) -> bool:
    """True iff :func:`select_parts_for_turn` will take its resumed-delta
    branch for ``(state, split)``.

    Single source of truth for "is this a resumed delta turn?" — mirrors
    the branch decision below (NOT stateless, state present, physical
    session established). Callers that need to reason about the delta path
    *before* inspecting a full :class:`SelectionResult` (and tests guarding
    against drift) read this instead of re-deriving the condition.
    """
    return (
        split is not PromptSessionSplit.STATELESS
        and state is not None
        and state.session_id is not None
    )


def select_parts_for_turn(
    envelope: PromptRenderEnvelope,
    state: PromptSessionState | None,
    split: PromptSessionSplit,
    *,
    delta_droppable_part_ids: frozenset[str] = frozenset(),
) -> SelectionResult:
    """Decide which envelope parts to send on a resumed turn.

    Pure function — no I/O, no envelope or state mutation. The
    returned :class:`SelectionResult` carries a candidate
    ``state_update`` the caller applies **only after** a
    successful agent invocation; failed invocations leave state
    untouched.

    Decision order:

    1. ``split == STATELESS`` → full render, ``state_update=None``.
       Stateless has no reusable physical key (M5) and therefore
       no reusable state.
    2. ``state is None`` → full render, ``state_update=None``. M7
       owns creating state under a
       :class:`~pipeline.prompts.session.PhysicalSessionKey`; M6
       does not invent one.
    3. ``state.session_id is None`` (fresh physical session) →
       full render, candidate ``state_update`` whose
       ``sent_part_keys`` includes every selected part.
    4. Otherwise (resumed) → delta render. Always include volatile
       parts and unseen part keys; force-resend role / phase /
       contract parts when the corresponding active anchor flipped;
       omit other stable parts already in ``sent_part_keys``.
       Version bumps land in the unseen branch via
       :func:`part_session_key`.
    """
    # ``delta_droppable_part_ids`` is honoured ONLY on the resumed-delta
    # branch below — full renders (stateless / no state / fresh session)
    # ignore it, so the original task is always on the wire on round 1 and
    # in stateless mode.
    if not will_render_delta(state, split):
        if split is PromptSessionSplit.STATELESS or state is None:
            return _full_render_no_state_update(envelope)
        # state.session_id is None → fresh physical session
        return _full_render_with_candidate_update(envelope, state)
    return _delta_render(
        envelope, state, frozenset(delta_droppable_part_ids),
    )


__all__ = [
    "SelectionResult",
    "select_parts_for_turn",
    "will_render_delta",
]
