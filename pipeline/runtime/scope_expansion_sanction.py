# SPDX-License-Identifier: Apache-2.0
"""
pipeline/runtime/scope_expansion_sanction.py — deterministic scope-expansion
sanction projection (ADR 0112 §5).

This focused module hosts the single decision that maps a run's strictness
posture (``OperatingMode``) plus a classified out-of-plan change's signals onto
*what to do about it* — the sanction route. It is the seam ADR 0112 §5 calls
for: the ADR 0110 classifier (:mod:`pipeline.engine.scope_expansion`) stays a
pure *fact* producer (``notice`` / ``risk`` / ``blocker``, ``has_blocker`` is a
fact, not a verdict), and the *verdict* — continue / alert / phase-handoff /
halt — moves here, projected from the operating mode rather than hardcoded into
the classifier. This mirrors :mod:`pipeline.runtime.session_disposition` (a
deterministic projection of a resolved policy + signals).

**The matrix (ADR 0139).** :func:`decide` is a total function over the closed
``OperatingMode`` vocabulary and the three durable scope-expansion statuses.
Classification remains evidence, not a delivery verdict; the operating mode is
the sole strictness decision:

    fast      notice/risk/blocker → AUTO_CONTINUE (record → re-setup → continue)
    pro       notice/risk/blocker → AUTO_ALERT (continue + disclosure)
    governed  notice/risk/blocker → HANDOFF (operator delivery decision)

**Policy is a projection carrier, not an outcome.** The knob that lands on
``RunShape`` (:class:`~pipeline.runtime.run_shape.ScopeExpansionSanctionPolicy`)
carries the *posture* (``operating_mode``), not a baked
``ScopeExpansionSanction``. :func:`project_scope_expansion_sanction` builds that
carrier from a mode via an exhaustive table guarded at import — modelled on
``semantic_mode_defaults._DEFAULT_OPERATING_MODE`` — so a future ``OperatingMode``
member added without an entry fails loudly at import rather than silently
defaulting. The §5 routing is wholly derivable from ``operating_mode`` (no
separate per-status override table is needed), so the carrier holds just the
posture; the outcome is *always* recomputed by :func:`decide`, never stored.

**Pure by construction.** :func:`decide` validates its inputs, matches
exhaustively over the closed ``OperatingMode`` enum (a fall-through raises
loudly), performs no I/O, reads no profile JSON, and does **not** import the
``pipeline.engine`` classifier package — keeping this an inert runtime
value-object layer with no wrong-direction dependency on the engine. The three
durable status values are mirrored as bare strings (a ``ScopeExpansionStatus``
member, being a ``StrEnum``, stringifies/compares to exactly these); a
divergence guard lives in the tests, which may freely import the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.runtime.roles import ScopeExpansionSanction
from pipeline.runtime.run_shape import OperatingMode, ScopeExpansionSanctionPolicy

# Mirror of the three durable ``ScopeExpansionStatus`` values (ADR 0110). Kept
# as bare strings so this inert policy module does not import the engine
# classifier package (importing it would pull ``agents`` / ``core`` and risk an
# import cycle during ``pipeline.runtime`` initialisation). A ``StrEnum`` status
# member stringifies/compares to exactly these values; the tests pin that these
# constants equal the engine enum so the mirror cannot silently drift.
STATUS_NOTICE = "scope_expansion_notice"
STATUS_RISK = "scope_expansion_risk"
STATUS_BLOCKER = "scope_expansion_blocker"
_KNOWN_STATUSES = frozenset({STATUS_NOTICE, STATUS_RISK, STATUS_BLOCKER})


@dataclass(frozen=True)
class ScopeExpansionDisposition:
    """Result of the scope-expansion sanction projection.

    Mirror of :class:`pipeline.runtime.session_disposition.SessionDisposition`.

    ``sanction`` is the chosen route the caller acts on. ``alert`` is whether
    the route raises an operator-visible alert (``True`` for ``AUTO_ALERT`` /
    ``HANDOFF``, ``False`` for a silent ``AUTO_CONTINUE``).
    ``reason`` is a short human-readable rationale for trace metadata and
    debugging; it is informational only and never parsed.
    """

    sanction: ScopeExpansionSanction
    reason: str
    alert: bool


# Exhaustive sanction-policy projection table. Every ``OperatingMode`` member
# must appear exactly once; each maps to a posture carrier (not an outcome).
# The §5 routing is fully derivable from the mode, so the carrier holds only
# the posture — see the module docstring's "policy is a projection carrier".
_SANCTION_POLICY_BY_MODE: dict[OperatingMode, ScopeExpansionSanctionPolicy] = {
    OperatingMode.FAST: ScopeExpansionSanctionPolicy(
        operating_mode=OperatingMode.FAST,
        notes="fast: benign scope expansion auto-sanctioned (notice); no pause",
    ),
    OperatingMode.PRO: ScopeExpansionSanctionPolicy(
        operating_mode=OperatingMode.PRO,
        notes="pro: every scope expansion continues with an operator alert",
    ),
    OperatingMode.GOVERNED: ScopeExpansionSanctionPolicy(
        operating_mode=OperatingMode.GOVERNED,
        notes="governed: any scope expansion alerts and routes to phase-handoff",
    ),
}

# Completeness guard: the table must cover the closed enum exactly. If a member
# is added to ``OperatingMode`` without a mapping here, this fails at import
# time instead of producing a silent default at call time (mirrors the
# ``semantic_mode_defaults`` import-time partition guard).
_missing = set(OperatingMode) - set(_SANCTION_POLICY_BY_MODE)
if _missing:  # pragma: no cover - guarded by test coverage of the table
    raise AssertionError(
        "scope_expansion_sanction: missing sanction policy for "
        f"{sorted(m.value for m in _missing)}"
    )
del _missing


def project_scope_expansion_sanction(
    operating_mode: OperatingMode,
) -> ScopeExpansionSanctionPolicy:
    """Project an ``OperatingMode`` onto its sanction *policy* carrier.

    Pure deterministic projection over the closed ``OperatingMode`` vocabulary
    (see the module table). Returns a posture carrier; the actual route is
    computed by :func:`decide`, never stored on the carrier. Performs no I/O.

    Raises
    ------
    KeyError
        If ``operating_mode`` is not a mapped ``OperatingMode`` member. The
        exhaustive-table import guard makes this unreachable for valid members;
        it surfaces an unmapped member explicitly rather than guessing.
    """

    return _SANCTION_POLICY_BY_MODE[operating_mode]


def decide(
    *,
    status: str,
    operating_mode: OperatingMode,
    has_active_waiver: bool,
) -> ScopeExpansionDisposition:
    """Project a classified out-of-plan change onto a sanction route (ADR 0112 §5).

    Pure deterministic projection (see the module rule). Performs no I/O.

    Parameters
    ----------
    status:
        The ADR 0110 classifier status of the change. Accepts a
        :class:`~pipeline.engine.scope_expansion.ScopeExpansionStatus` member
        (a ``StrEnum``, so it stringifies to one of the three durable values)
        or the bare value string. An unknown status fails fast.
    operating_mode:
        The run's strictness posture (the carried sanction policy's mode). Must
        be an ``OperatingMode`` member — there is no string/legacy fallback.
    has_active_waiver:
        Whether an operator ``continue_with_waiver`` is active. When ``True``
        the gate is fully disarmed (``AUTO_CONTINUE``) in every mode.

    Raises
    ------
    TypeError
        If ``operating_mode`` is not an ``OperatingMode`` or
        ``has_active_waiver`` is not ``bool``. Fail-fast rather than guessing.
    ValueError
        If ``status`` is not one of the three durable scope-expansion statuses.
    """
    if not isinstance(operating_mode, OperatingMode):
        raise TypeError(
            "scope_expansion_sanction.decide: operating_mode must be an "
            f"OperatingMode, got {type(operating_mode).__name__}"
        )
    if not isinstance(has_active_waiver, bool):
        raise TypeError(
            "scope_expansion_sanction.decide: has_active_waiver must be bool, "
            f"got {type(has_active_waiver).__name__}"
        )
    status_value = str(status)
    if status_value not in _KNOWN_STATUSES:
        raise ValueError(
            "scope_expansion_sanction.decide: unknown scope-expansion status "
            f"{status!r}; expected one of {sorted(_KNOWN_STATUSES)}"
        )

    # An active waiver fully disarms the gate in every mode. It remains the
    # single explicit operator escape hatch rather than a parallel route.
    if has_active_waiver:
        return ScopeExpansionDisposition(
            sanction=ScopeExpansionSanction.AUTO_CONTINUE,
            reason=(
                "active continue_with_waiver disarms the scope-expansion gate "
                "in every mode"
            ),
            alert=False,
        )

    # Mode-projected routing of the classifier fact. Category/evidence remain
    # visible in the durable assessment, but do not introduce a second policy
    # axis that can overrule the selected operating mode.
    match operating_mode:
        case OperatingMode.FAST:
            return ScopeExpansionDisposition(
                sanction=ScopeExpansionSanction.AUTO_CONTINUE,
                reason=(
                    f"fast: benign {status_value} auto-sanctioned "
                    "(record → re-setup → continue); no pause"
                ),
                alert=False,
            )
        case OperatingMode.PRO:
            return ScopeExpansionDisposition(
                sanction=ScopeExpansionSanction.AUTO_ALERT,
                reason=f"pro: {status_value} continues with an operator alert",
                alert=True,
            )
        case OperatingMode.GOVERNED:
            return ScopeExpansionDisposition(
                sanction=ScopeExpansionSanction.HANDOFF,
                reason=(
                    f"governed: any participant-add / scope expansion "
                    f"({status_value}) alerts and routes through phase-handoff "
                    "for operator sanction"
                ),
                alert=True,
            )

    # Completeness guard: an OperatingMode member added without a branch above
    # must fail loudly rather than silently defaulting.
    raise AssertionError(
        "scope_expansion_sanction.decide: unhandled routing for "
        f"operating_mode={operating_mode!r}, status={status_value!r}; the "
        "match over OperatingMode/status is not exhaustive"
    )


__all__ = [
    "STATUS_BLOCKER",
    "STATUS_NOTICE",
    "STATUS_RISK",
    "ScopeExpansionDisposition",
    "decide",
    "project_scope_expansion_sanction",
]
