# SPDX-License-Identifier: Apache-2.0
"""
pipeline/runtime/session_disposition.py — deterministic session-disposition
projection.

This focused module hosts the single decision that maps a resolved
*continuity policy* plus an invocation's follow-on signals onto whether its
agent call continues the prior provider session or starts fresh. It is the one
and only continuity *decision* site: callers resolve a
:class:`~pipeline.runtime.roles.SessionContinuity` member from the profile (the
per-phase ``session_continuity`` field, with role-level auxiliary overrides) and
hand it here together with the relevant follow-on signals. The policy lives on
the profile, not in this module.

**The rule.** :func:`decide` is a total function over the closed
:class:`SessionContinuity` vocabulary:

- ``fresh_only`` — always start fresh; no follow-on signal can continue.
- ``loop_continue`` — continue iff this is a loop follow-on (``loop_followon``);
  round 2+ of a planning/reviewing loop resumes, round 1 is fresh.
- ``same_zone_continue`` — continue iff the follow-on writes into the same
  physical write zone (``same_write_zone``); a cross-zone follow-on is fresh.

``operating_mode`` is part of the declared projection input. The current rule
does not relax continuation for any posture, so a stricter mode never *adds*
continuation; it is validated and reserved for a future posture knob.

**Pure by construction.** :func:`decide` matches exhaustively over the closed
:class:`SessionContinuity` enum: every member has a branch, and the fall-through
raises loudly so a future member added without a branch fails here rather than
silently defaulting. It performs no I/O, reads no profile JSON, and does not
import ``pipeline.profiles.loader`` — importing this module is side-effect free
with respect to the profile loader, profile JSON, git, and the environment.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.runtime.roles import SessionContinuity
from pipeline.runtime.run_shape import OperatingMode


@dataclass(frozen=True)
class SessionDisposition:
    """Result of the session-disposition projection.

    ``continue_session`` is the boolean handed to the runtime seam (the
    ``--resume`` probe). ``reason`` is a short human-readable rationale for
    trace metadata and debugging; it is informational only and never parsed.
    """

    continue_session: bool
    reason: str


def decide(
    *,
    policy: SessionContinuity,
    same_write_zone: bool,
    loop_followon: bool,
    operating_mode: OperatingMode,
) -> SessionDisposition:
    """Project a resolved continuity policy + follow-on signals onto a
    :class:`SessionDisposition`.

    Pure deterministic projection over the closed :class:`SessionContinuity`
    vocabulary (see the module rule). Performs no I/O.

    Parameters
    ----------
    policy:
        The resolved per-phase continuity policy. Must be a
        ``SessionContinuity`` member — there is no string/legacy fallback. The
        caller resolves it from the profile (and any role-level auxiliary
        override) before calling.
    same_write_zone:
        Whether this invocation's follow-on writes into the same physical zone
        as the session it would resume. Only consulted under
        ``same_zone_continue``.
    loop_followon:
        Whether this invocation is a loop follow-on (round 2+ of the same
        planning/reviewing loop). Only consulted under ``loop_continue``.
    operating_mode:
        The run's strictness posture. Validated and reserved; the current rule
        never relaxes continuation for any posture.

    Raises
    ------
    TypeError
        If ``policy`` is not a ``SessionContinuity`` or ``operating_mode`` is
        not an ``OperatingMode``. Fail-fast rather than guessing a default.
    """
    if not isinstance(policy, SessionContinuity):
        raise TypeError(
            "session_disposition.decide: policy must be a SessionContinuity, "
            f"got {type(policy).__name__}"
        )
    if not isinstance(operating_mode, OperatingMode):
        raise TypeError(
            "session_disposition.decide: operating_mode must be an "
            f"OperatingMode, got {type(operating_mode).__name__}"
        )

    match policy:
        case SessionContinuity.FRESH_ONLY:
            return SessionDisposition(
                continue_session=False,
                reason=f"{policy.value}: always starts a fresh session",
            )
        case SessionContinuity.LOOP_CONTINUE:
            if loop_followon:
                return SessionDisposition(
                    continue_session=True,
                    reason=(
                        f"{policy.value}: loop follow-on continues the prior "
                        "session"
                    ),
                )
            return SessionDisposition(
                continue_session=False,
                reason=f"{policy.value}: first loop round → fresh",
            )
        case SessionContinuity.SAME_ZONE_CONTINUE:
            if same_write_zone:
                return SessionDisposition(
                    continue_session=True,
                    reason=(
                        f"{policy.value}: same-write-zone follow-on continues "
                        "the prior session"
                    ),
                )
            return SessionDisposition(
                continue_session=False,
                reason=(
                    f"{policy.value}: follow-on crosses write zones → fresh"
                ),
            )

    # Completeness guard: a future SessionContinuity member added without a
    # branch above must fail loudly here rather than silently defaulting. This
    # carries the spirit of the old import-time partition guard into the match.
    raise AssertionError(
        "session_disposition.decide: unhandled SessionContinuity member "
        f"{policy!r}; match over SessionContinuity is not exhaustive"
    )


__all__ = ["SessionDisposition", "decide"]
