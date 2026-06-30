# SPDX-License-Identifier: Apache-2.0
"""Single source of truth for release-verdict classification (ADR 0115 slice 2).

Before this module the release verdict (``APPROVED`` / ``REJECTED`` / other) and
its derived guards were re-judged independently in at least six places —
``pipeline/engine/commit_delivery.py``, ``pipeline/project/run.py``,
``pipeline/project/finalization.py`` (``_done_phase_outcome``), and
``sdk/run_control/delivery.py`` (``_is_rejected_release_gate`` / ``decide_delivery``
/ ``delivery_decision_state``) — each with its own ``!= "APPROVED"`` /
``== "REJECTED"`` literal. Divergent re-judgments let one surface flip a verdict
guard the others did not. This module owns the classification; every consumer
reads it.

Scope: the mono release verdict (ADR 0115 slice 2) plus the cross-delivery
per-child override guard (``pipeline/cross_project/cross_delivery.py``), folded
onto this source in slice 4. The cross *aggregation* verdict (``_aggregate``)
stays cross-specific and is intentionally not collapsed here.

Pure string logic, no imports — ``pipeline/run_state`` must not depend on runtime
or SDK, so this module is safe to import from both ``pipeline/*`` and ``sdk/*``.
"""
from __future__ import annotations

#: The two canonical release verdicts a release gate emits.
APPROVED = "APPROVED"
REJECTED = "REJECTED"


def normalize_verdict(verdict: object) -> str:
    """Coerce a raw verdict to its canonical upper-cased token (``""`` if absent).

    Release verdicts are emitted uppercase by the release parser; normalising
    here makes every consumer agree on case/whitespace instead of some calling
    ``.upper()`` and others comparing the raw string.
    """
    if not isinstance(verdict, str):
        verdict = "" if verdict is None else str(verdict)
    return verdict.strip().upper()


def is_approved(verdict: object) -> bool:
    """True iff the verdict is exactly ``APPROVED`` (canonical)."""
    return normalize_verdict(verdict) == APPROVED


def is_rejected(verdict: object) -> bool:
    """True iff the verdict is exactly ``REJECTED`` (canonical).

    This is the strict-rejected test (the recovery-lineage / DONE-chip mapping),
    distinct from :func:`is_release_blocked` — a non-``APPROVED`` verdict that is
    not literally ``REJECTED`` (e.g. empty / unknown) is *blocked* but not
    *rejected*.
    """
    return normalize_verdict(verdict) == REJECTED


def is_release_blocked(verdict: object, *, empty_blocks: bool) -> bool:
    """True when a non-``APPROVED`` verdict should block automatic delivery.

    The single non-approved guard. ``empty_blocks`` carries the one legitimate
    per-consumer difference (parity-preserving):

    - ``empty_blocks=True`` — an empty/missing verdict counts as blocked. Used by
      ``commit_delivery.resolve_commit_delivery`` (``profile_gates_release and
      verdict != "APPROVED"`` — a gating profile with no recorded APPROVED is
      blocked).
    - ``empty_blocks=False`` — an empty/missing verdict is NOT blocked (there is
      no verdict to refuse on). Used by ``run.py``'s ``rejected_release`` and the
      SDK delivery guards (``bool(verdict) and verdict != "APPROVED"``).

    A present non-``APPROVED`` verdict blocks under both modes.
    """
    nv = normalize_verdict(verdict)
    if not nv:
        return empty_blocks
    return nv != APPROVED
