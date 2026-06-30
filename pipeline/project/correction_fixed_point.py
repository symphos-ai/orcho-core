"""Deterministic fixed-point guard for the correction follow-up loop (ADR 0098).

A correction round can get stuck: final acceptance rejects for the same release
blockers as the parent, the round makes no relevant progress on the flagged
files or evidence, and the run still finishes looking like "try another
correction". That is a fixed point — same findings, same files, same outcome.

This module owns the **pure** half of detecting that: normalizing the durable
blocker identity carried in ``session['phases']['final_acceptance']`` (release
blockers, verification gaps, and the engine receipt backstop) into a stable
``frozenset[str]`` of keys, and comparing a parent round's session against the
child round it produced. The comparison takes the two progress facts —
``code_changed`` and ``receipts_changed`` — by injection, so this module never
touches the filesystem, a subprocess, or a provider. The driver
(:mod:`pipeline.project.correction_followup`) reads the artifacts and feeds the
facts in.

Identity is built from durable evidence, never from provider prose (critique /
short_summary): a blocker keyed on its rendered critique text would churn on
every reword. The condition to fire is strictly conjunctive and conservative —
any sign of progress (a changed identity set, a changed diff, fresher receipts)
suppresses the guard. Non-goals: no auto-waiver, no final-acceptance policy
loosening, no LLM classifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FixedPointVerdict",
    "blocker_identity_set",
    "evaluate_fixed_point",
    "render_non_convergence_block",
]


def _norm(value: Any) -> str:
    """Collapse a value to a stable, whitespace-normalized lowercase token."""
    return " ".join(str(value or "").split()).strip().lower()


def _final_acceptance_entry(session: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the ``final_acceptance`` phase record from a run session.

    Mirrors ``correction_followup._final_acceptance_entry`` but also tolerates
    the list-of-attempts shape (taking the last mapping) so the identity is
    read from the same durable record the finalization summary reads.
    """
    if not isinstance(session, Mapping):
        return {}
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        return {}
    entry = phases.get("final_acceptance")
    if isinstance(entry, Mapping):
        return entry
    if isinstance(entry, list):
        for item in reversed(entry):
            if isinstance(item, Mapping):
                return item
    return {}


def _blocker_key(blocker: Mapping[str, Any]) -> str:
    """Normalized identity for one release blocker.

    Keyed on the stable structural fields — the blocker code (``id`` / ``code``
    falling back to ``title``), severity, and affected file — never on the prose
    ``body`` / ``why_blocks_release``, so a reworded explanation of the same
    blocker still collapses to the same key.
    """
    code = _norm(blocker.get("id") or blocker.get("code") or blocker.get("title"))
    severity = _norm(blocker.get("severity"))
    file = _norm(blocker.get("file") or blocker.get("path"))
    return "|".join(("final_acceptance", "release_blocker", code, severity, file))


def _gap_key(gap: Mapping[str, Any], *, category: str) -> str:
    """Normalized identity for a verification gap / engine receipt backstop.

    Both the model-emitted ``verification_gaps`` and the engine
    ``engine_backstop.gaps`` share the ``{risk, missing_evidence,
    required_check}`` shape. ``required_check`` is the actionable command /
    receipt identity (the run line for a missing/stale/failed receipt), so it is
    the stable key; ``risk`` is a prose fallback when no check is present.
    """
    check = _norm(gap.get("required_check")) or _norm(gap.get("risk"))
    return "|".join(("final_acceptance", category, check))


def blocker_identity_set(session: Mapping[str, Any] | None) -> frozenset[str]:
    """Normalize the durable blocker identity of a run into a set of keys.

    Sources, in order: ``final_acceptance.release_blockers`` (structural blocker
    identity), ``final_acceptance.verification_gaps`` (reviewer-flagged checks),
    and ``final_acceptance.engine_backstop.gaps`` (the deterministic
    missing/stale/failed required-receipt backstop). Robust to missing fields
    and unexpected shapes — anything non-mapping is skipped, never raised on.
    """
    fa = _final_acceptance_entry(session)
    keys: set[str] = set()

    blockers = fa.get("release_blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                keys.add(_blocker_key(blocker))

    gaps = fa.get("verification_gaps")
    if isinstance(gaps, list):
        for gap in gaps:
            if isinstance(gap, Mapping):
                keys.add(_gap_key(gap, category="verification_gap"))

    backstop = fa.get("engine_backstop")
    if isinstance(backstop, Mapping):
        engine_gaps = backstop.get("gaps")
        if isinstance(engine_gaps, list):
            for gap in engine_gaps:
                if isinstance(gap, Mapping):
                    keys.add(_gap_key(gap, category="engine_gap"))

    return frozenset(keys)


def _is_rejected_with_blockers(
    session: Mapping[str, Any] | None, ids: frozenset[str],
) -> bool:
    """True when final acceptance rejected AND carries a non-empty identity set.

    "Rejected" reads the durable verdict: an explicit ``REJECTED`` verdict, or
    ``ship_ready`` / ``approved`` set to ``False``. An empty identity set is not
    rejected-with-blockers — there is nothing to repeat.
    """
    if not ids:
        return False
    fa = _final_acceptance_entry(session)
    verdict = fa.get("verdict")
    if isinstance(verdict, str) and verdict.upper() == "REJECTED":
        return True
    if fa.get("ship_ready") is False:
        return True
    return fa.get("approved") is False


@dataclass(frozen=True)
class FixedPointVerdict:
    """Structured outcome of :func:`evaluate_fixed_point`.

    ``is_fixed_point`` is True only when the conjunctive condition holds; in
    that case ``repeated`` is the sorted tuple of the shared blocker identities
    and ``reason`` is a short operator-readable explanation. When False,
    ``repeated`` is empty and ``reason`` names which guard suppressed it.
    """

    is_fixed_point: bool
    repeated: tuple[str, ...]
    reason: str


def evaluate_fixed_point(
    parent_session: Mapping[str, Any] | None,
    child_session: Mapping[str, Any] | None,
    *,
    code_changed: bool,
    receipts_changed: bool,
) -> FixedPointVerdict:
    """Decide whether a correction child is a non-converging repeat of its parent.

    Pure: no IO, no subprocess, no provider. The two progress facts are injected
    by the driver. The condition is strictly conjunctive and conservative:

    1. both parent and child are rejected-with-blockers (non-empty identity set
       and a rejecting verdict);
    2. the normalized identity sets are non-empty and equal — a changed identity
       (a blocker fixed, removed, or newly introduced) counts as progress;
    3. there is no progress signal — neither a changed child diff
       (``code_changed``) nor fresher/passing receipts (``receipts_changed``).

    Any failure of the conjunction yields ``is_fixed_point=False`` — when in
    doubt, treat progress as present and let the loop continue.
    """
    parent_ids = blocker_identity_set(parent_session)
    child_ids = blocker_identity_set(child_session)

    if not _is_rejected_with_blockers(parent_session, parent_ids):
        return FixedPointVerdict(
            False, (), "parent is not rejected-with-blockers",
        )
    if not _is_rejected_with_blockers(child_session, child_ids):
        return FixedPointVerdict(
            False, (), "child is not rejected-with-blockers",
        )
    if not child_ids or child_ids != parent_ids:
        return FixedPointVerdict(
            False, (), "blocker identity changed since parent run",
        )
    if code_changed:
        return FixedPointVerdict(
            False, (), "child changed the flagged diff",
        )
    if receipts_changed:
        return FixedPointVerdict(
            False, (), "child produced fresher verification receipts",
        )

    repeated = tuple(sorted(child_ids))
    return FixedPointVerdict(
        True,
        repeated,
        (
            "correction repeated the same release blockers with no relevant "
            "diff or evidence progress since the parent run"
        ),
    )


def render_non_convergence_block(
    *,
    repeated: tuple[str, ...] | list[str],
    parent_run_id: str,
    child_run_id: str,
) -> str:
    """Render the operator block printed when the fixed-point guard fires.

    Deterministic Orcho decision text (not a reviewer hallucination): it names
    the repeated blocker identities, both run ids, that no blocker evidence
    changed, and the legal next actions the agent itself cannot take.
    """
    repeated_str = ", ".join(repeated) if repeated else "(none)"
    return "\n".join((
        "Correction is not converging.",
        f"Repeated blockers: {repeated_str}",
        f"Parent run: {parent_run_id}",
        f"Child run: {child_run_id}",
        "No relevant blocker evidence changed since parent run.",
        "Human decision required: retry with new instructions, "
        "approve/waive, or halt.",
    ))
