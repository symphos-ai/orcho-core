"""
pipeline/runtime/semantic_mode_defaults.py — deterministic default-mode
projection for the Stage C semantic vocabulary.

This focused module hosts a single pure function,
``default_operating_mode``, that maps each ``SemanticProfile`` work kind
onto its default ``OperatingMode`` strictness posture. The table is the
Stage C product decision:

    small_task       → fast
    feature          → fast
    complex_feature  → pro
    planning         → pro
    code_review      → pro
    delivery_audit   → pro
    research         → fast
    refactor         → pro
    migration        → pro

``governed`` is intentionally never a default — it is an explicit opt-in
posture, not something a work kind selects on its own.

**Pure by construction.** The projection is a total function over the
closed ``SemanticProfile`` enum: a lookup against an exhaustive dict whose
completeness is asserted at import against ``SemanticProfile``'s members,
so a future enum member added without a mapping fails loudly here rather
than silently defaulting. It performs no I/O, reads no profile JSON, and
does not import ``pipeline.profiles.loader`` — importing this module stays
side-effect free with respect to the profile loader, profile JSON, git,
and the environment.
"""

from __future__ import annotations

from pipeline.runtime.run_shape import OperatingMode, SemanticProfile

# Exhaustive Stage C default-mode table. Every ``SemanticProfile`` member
# must appear exactly once; ``governed`` deliberately never appears as a
# default value (it is an explicit opt-in posture).
_DEFAULT_OPERATING_MODE: dict[SemanticProfile, OperatingMode] = {
    SemanticProfile.SMALL_TASK: OperatingMode.FAST,
    SemanticProfile.FEATURE: OperatingMode.FAST,
    SemanticProfile.COMPLEX_FEATURE: OperatingMode.PRO,
    SemanticProfile.PLANNING: OperatingMode.PRO,
    SemanticProfile.CODE_REVIEW: OperatingMode.PRO,
    SemanticProfile.DELIVERY_AUDIT: OperatingMode.PRO,
    SemanticProfile.RESEARCH: OperatingMode.FAST,
    SemanticProfile.REFACTOR: OperatingMode.PRO,
    SemanticProfile.MIGRATION: OperatingMode.PRO,
}

# Completeness guard: the table must cover the closed enum exactly. If a
# member is added to ``SemanticProfile`` without a mapping here, this fails
# at import time instead of producing a silent default at call time.
_missing = set(SemanticProfile) - set(_DEFAULT_OPERATING_MODE)
if _missing:  # pragma: no cover - guarded by test coverage of the table
    raise AssertionError(
        "semantic_mode_defaults: missing default OperatingMode for "
        f"{sorted(p.value for p in _missing)}"
    )
del _missing


def default_operating_mode(profile: SemanticProfile) -> OperatingMode:
    """Return the default ``OperatingMode`` for a semantic work kind.

    Pure deterministic projection over the closed ``SemanticProfile``
    vocabulary (see the module table). ``governed`` is never returned as a
    default. Performs no I/O.

    Raises
    ------
    KeyError
        If ``profile`` is not a mapped ``SemanticProfile`` member. The
        exhaustive-table import guard makes this unreachable for valid
        members; it surfaces an unmapped member explicitly rather than
        guessing a default.
    """

    return _DEFAULT_OPERATING_MODE[profile]


__all__ = ["default_operating_mode"]
