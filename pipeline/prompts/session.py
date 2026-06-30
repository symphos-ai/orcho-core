"""Prompt-session primitives for ADR-0026 session-aware rendering.

This module owns Orcho-side bookkeeping for what prompt parts have
already been sent into a physical runtime session. M5 adds the
data model and pure key-selection logic; M6 builds the delta
selector on top, and M7 wires both into the validate-plan adapter
(the first phase to use them at runtime).

The vocabulary is intentionally kept distinct from
:mod:`agents.protocols`:

- :class:`agents.protocols.SessionMode` (``AUTO`` / ``STATELESS`` /
  ``CHAIN`` / ``HYBRID``) governs the **runtime bridge** policy:
  whether and how the agent runtime chains successive calls into one
  provider session.
- :class:`PromptSessionSplit` (``STATELESS`` / ``PER_PHASE`` /
  ``PER_ROLE`` / ``COMMON``) governs the **prompt-part reuse**
  policy: how Orcho groups physical sessions for the purpose of
  omitting already-sent stable parts on resumed turns.

The two names must not be conflated. A regression test in
``test_session_state.py`` asserts they remain distinct enums.

Nothing in this module touches :mod:`agents.protocols`. Per repo
notes the runtime Protocol redesign is deferred and will land as a
side-effect of the prompt composer; M5 is purely additive Orcho-
side state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from pipeline.prompts.types import PromptPart


class PromptSessionSplit(StrEnum):
    """How Orcho groups physical sessions for prompt-part reuse.

    Distinct from :class:`agents.protocols.SessionMode`, which
    governs the runtime bridge between successive provider calls.
    Do not import or alias the runtime enum from this module.

    - :data:`STATELESS` — no reusable prompt-part state. Every
      invocation re-renders every selected part.
    - :data:`PER_PHASE` — one reusable session per phase. The same
      reviewer phase resumes within one run; switching phases
      starts a fresh reusable session.
    - :data:`PER_ROLE` — one reusable session per prompt role.
      Multiple phases that share a role may reuse the same prefix.
    - :data:`COMMON` — one reusable session per run, still keyed
      by runtime and model. Most aggressive sharing.
    """

    STATELESS = "stateless"
    PER_PHASE = "per_phase"
    PER_ROLE = "per_role"
    COMMON = "common"


@dataclass(frozen=True)
class PhysicalSessionKey:
    """Orcho-side identity of one reusable physical prompt session.

    Two invocations sharing the same key may reuse the runtime's
    cached prefix for parts already in
    :class:`PromptSessionState.sent_part_keys`. Different keys
    produce isolated state, so a model or runtime change forces a
    full render.

    ``model_key`` is strict by default — two physically distinct
    models never share a key even when their families overlap.
    Future capability negotiation may relax this for runtimes that
    advertise compatible cache backplanes; M5 does not.
    """

    run_id: str
    runtime: str
    model_key: str
    scope: str


@dataclass(frozen=True)
class PromptSessionState:
    """What Orcho has already sent into one physical session.

    Pure data; mutation goes through the helpers below, each of
    which returns a fresh instance (the dataclass is frozen). M6's
    delta selector reads ``sent_part_keys`` plus the active role /
    phase / contract anchors to decide which envelope parts can be
    omitted on a resumed turn.

    ``sent_part_keys`` stores composite ``id@version`` strings via
    :func:`part_session_key`. Storing only ``id`` would silently
    treat a version-bumped part as already-sent and break the
    ADR-0026 "version change = unseen" rule.
    """

    key: PhysicalSessionKey
    session_id: str | None = None
    sent_part_keys: frozenset[str] = field(default_factory=frozenset)
    active_role_id: str | None = None
    active_phase_id: str | None = None
    active_contract_ids: frozenset[str] = field(default_factory=frozenset)


def part_session_key(part: PromptPart) -> str:
    """Canonical sent-part identifier — ``"{id}@{version or 0}"``.

    Used by :class:`PromptSessionState.sent_part_keys` and by M6's
    delta selector. Including ``version`` here means a part bumped
    to a new version with the same id naturally registers as
    "unseen" — the M6 selector compares composite keys, not bare
    ids.
    """
    version = part.version if part.version is not None else 0
    return f"{part.id}@{version}"


def make_session_key(
    *,
    run_id: str,
    runtime: str,
    model_key: str,
    split: PromptSessionSplit,
    role: str | None = None,
    phase: str | None = None,
) -> PhysicalSessionKey | None:
    """Build a :class:`PhysicalSessionKey` for ``split``, or ``None``.

    Returns ``None`` for :data:`PromptSessionSplit.STATELESS` —
    stateless means **no reusable physical prompt session key**.
    Storing state under a sentinel "stateless" key would invite
    accidental reuse; the M6/M7 callers must explicitly handle
    "no session" instead.

    All non-stateless modes always include ``runtime`` and
    ``model_key`` in the key, so a runtime or model change forces
    a full render in every reusable mode.

    ``PER_PHASE`` requires ``phase``; ``PER_ROLE`` requires
    ``role``. Missing required arguments raise :class:`ValueError`.
    """
    if split is PromptSessionSplit.STATELESS:
        return None
    if split is PromptSessionSplit.PER_PHASE:
        if not phase:
            raise ValueError(
                "PromptSessionSplit.PER_PHASE requires a non-empty 'phase'.",
            )
        scope = f"per_phase:{phase}"
    elif split is PromptSessionSplit.PER_ROLE:
        if not role:
            raise ValueError(
                "PromptSessionSplit.PER_ROLE requires a non-empty 'role'.",
            )
        scope = f"per_role:{role}"
    elif split is PromptSessionSplit.COMMON:
        scope = "common"
    else:  # pragma: no cover — exhaustive over StrEnum members
        raise ValueError(f"unknown PromptSessionSplit: {split!r}")
    return PhysicalSessionKey(
        run_id=run_id,
        runtime=runtime,
        model_key=model_key,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Pure mutation helpers. Each returns a new ``PromptSessionState``;
# the input is never mutated (frozen dataclass). M7 will compose
# these around a successful agent invocation: record what was sent,
# update the provider session id, advance the active-context
# anchors. The state is committed only after a successful call so a
# failed invocation cannot corrupt the cache view.
# ---------------------------------------------------------------------------


def record_sent_parts(
    state: PromptSessionState,
    parts: Iterable[PromptPart],
) -> PromptSessionState:
    """Add the keys of *parts* to ``state.sent_part_keys``.

    Existing keys are preserved (set union). Identity is per
    :func:`part_session_key`, i.e. ``id + version``.
    """
    new_keys = state.sent_part_keys | {part_session_key(p) for p in parts}
    return replace(state, sent_part_keys=new_keys)


def with_provider_session_id(
    state: PromptSessionState,
    session_id: str | None,
) -> PromptSessionState:
    """Update the runtime provider's session handle on *state*."""
    return replace(state, session_id=session_id)


def with_active_role(
    state: PromptSessionState,
    role_id: str | None,
) -> PromptSessionState:
    """Set the active role anchor on *state*.

    The M6 selector reads this to decide whether a role part needs
    re-sending: a role-id change means the previously-sent persona
    no longer applies and the new role part must be included.
    """
    return replace(state, active_role_id=role_id)


def with_active_phase(
    state: PromptSessionState,
    phase_id: str | None,
) -> PromptSessionState:
    """Set the active phase anchor on *state* (mirror of role)."""
    return replace(state, active_phase_id=phase_id)


def with_active_contracts(
    state: PromptSessionState,
    contract_ids: frozenset[str] | Iterable[str],
) -> PromptSessionState:
    """Set the active contract anchors on *state*.

    Any change in the set forces the M6 selector to resend the
    corresponding contract parts on the next turn.
    """
    if not isinstance(contract_ids, frozenset):
        contract_ids = frozenset(contract_ids)
    return replace(state, active_contract_ids=contract_ids)


__all__ = [
    "PhysicalSessionKey",
    "PromptSessionSplit",
    "PromptSessionState",
    "make_session_key",
    "part_session_key",
    "record_sent_parts",
    "with_active_contracts",
    "with_active_phase",
    "with_active_role",
    "with_provider_session_id",
]
