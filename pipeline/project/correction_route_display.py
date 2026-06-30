# SPDX-License-Identifier: Apache-2.0
"""Read-only presentation of the correction-route decision.

This module turns the durable ``correction_triage`` evidence into compact,
operator-facing strings: one full *decision* line emitted right after triage
completes, and one compact *summary* line for the DONE / HALTED finalization
block. It is deliberately a pure formatting layer — no I/O, no ``print``, and
no imports from the runtime, orchestrator, or finalization modules. The only
domain dependency is :func:`pipeline.project.correction_route.derive_correction_route`,
which is itself a pure derivation.

Consumers decide *where* and *how* (terminal vs. progress.log, color tone) to
render these strings; this module only decides their text and a couple of
semantic flags (``halted`` drives an amber tone downstream).
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pipeline.project.correction_route import derive_correction_route

#: Maximum width of a compact reason string before it is shortened. Keeps the
#: route line to one or two terminal lines and never embeds reviewer or
#: final-acceptance bodies.
_REASON_WIDTH = 160


@dataclass(frozen=True)
class CorrectionRouteDisplay:
    """A single rendered correction-route line.

    ``text`` is the ready-to-render string. ``halted`` is true only for the
    blocked / halting route — consumers use it to pick an amber tone instead
    of the neutral/dim tone used for shortcut skips. ``kind`` mirrors the
    triage classification for any consumer that wants to branch on it.
    """

    text: str
    halted: bool
    kind: str


def _compact_reason(record: Mapping[str, Any]) -> str:
    """Compact summary + blocker digest for a blocked route, truncated.

    Combines the triage ``summary`` with a small blocker digest (count plus
    the first named blocker) and shortens the whole thing to ``_REASON_WIDTH``
    so no long body leaks into the route line.
    """
    summary = str(record.get("summary") or "").strip()
    parts: list[str] = []
    if summary:
        parts.append(summary)

    blockers = record.get("blockers")
    if isinstance(blockers, (list, tuple)):
        named = [str(b).strip() for b in blockers if str(b).strip()]
        if named:
            head = named[0]
            if len(named) == 1:
                parts.append(f"1 blocker: {head}")
            else:
                parts.append(f"{len(named)} blockers; first: {head}")

    text = " — ".join(parts) or "(no triage summary recorded)"
    return textwrap.shorten(text, width=_REASON_WIDTH, placeholder="…")


def _skip_phase_list(skip_phases: Any) -> str:
    """Render skip phases in the same sorted order used by ``to_evidence``."""
    if isinstance(skip_phases, (list, tuple, set, frozenset)):
        names = sorted(str(p).strip() for p in skip_phases if str(p).strip())
        return "/".join(names)
    return ""


def format_correction_route_decision(
    record: Mapping[str, Any] | None,
) -> CorrectionRouteDisplay | None:
    """Full route-decision line, emitted right after ``correction_triage``.

    Returns ``None`` when ``record`` is not a mapping (no triage evidence →
    not a correction-routed run). Otherwise derives the route and renders:

    - ``gate_rerun`` / ``contract_ack`` — ``"Correction route: <kind> →
      skipping <phases>"`` with phases sorted as in ``to_evidence``.
    - ``blocked`` (and any unknown kind, defensively halting) —
      ``"Correction route: <kind> → halting before implement; <reason>"``
      with a compact summary + blocker digest.
    - ``code_fix`` — ``"Correction route: code_fix → full correction path"``.
    """
    if not isinstance(record, Mapping):
        return None

    route = derive_correction_route(record)
    if route is None:
        return None

    kind = route.kind
    if route.halt:
        reason = _compact_reason(record)
        text = (
            f"Correction route: {kind} → halting before implement; {reason}"
        )
        return CorrectionRouteDisplay(text=text, halted=True, kind=kind)

    if route.skip_phases:
        phases = _skip_phase_list(route.skip_phases)
        text = f"Correction route: {kind} → skipping {phases}"
        return CorrectionRouteDisplay(text=text, halted=False, kind=kind)

    # code_fix: full correction path, emitted explicitly so the invariant
    # "a route line exists iff there is triage evidence" holds uniformly.
    text = f"Correction route: {kind} → full correction path"
    return CorrectionRouteDisplay(text=text, halted=False, kind=kind)


def _final_acceptance_outcome(phases: Mapping[str, Any]) -> str:
    """Outcome token for ``final_acceptance``, by existing conventions.

    ``APPROVED`` verdict or ``ship_ready is True`` → ``"ok"``; ``REJECTED``
    verdict or ``ship_ready is False`` → ``"rejected"``; no record (or an
    indeterminate one) → ``"pending"``. This mirrors ``_attempt_approved`` in
    finalization without importing that module's private helpers.
    """
    value = phases.get("final_acceptance")
    if isinstance(value, list):
        attempts = [item for item in value if isinstance(item, Mapping)]
        attempt: Mapping[str, Any] = attempts[-1] if attempts else {}
    elif isinstance(value, Mapping):
        attempt = value
    else:
        attempt = {}

    if not attempt:
        return "pending"

    verdict = attempt.get("verdict")
    if isinstance(verdict, str) and verdict.upper() == "APPROVED":
        return "ok"
    if attempt.get("ship_ready") is True:
        return "ok"
    if isinstance(verdict, str) and verdict.upper() == "REJECTED":
        return "rejected"
    if attempt.get("ship_ready") is False:
        return "rejected"
    return "pending"


def format_correction_route_summary(
    phases: Mapping[str, Any] | None,
) -> CorrectionRouteDisplay | None:
    """Compact route summary for the DONE / HALTED finalization block.

    Returns ``None`` when ``phases`` is not a mapping or carries no
    ``correction_triage`` record (a non-correction run gets no route line).

    The route source is the stamped ``correction_triage['route']`` evidence
    when present; otherwise the route is derived from the triage record
    itself (the blocked / halted path, where the route dict is not stamped).
    Renders:

    - ``gate_rerun`` / ``contract_ack`` — ``"Correction route: <kind> →
      skipped <phases>; final_acceptance=<outcome>"``.
    - ``blocked`` (and unknown halting kinds) — ``"Correction route: <kind>
      → halted before implement; <reason>"`` with ``halted=True``.
    - ``code_fix`` — ``"Correction route: code_fix → full correction path"``.
    """
    if not isinstance(phases, Mapping):
        return None

    triage = phases.get("correction_triage")
    if not isinstance(triage, Mapping):
        return None

    stamped = triage.get("route")
    if isinstance(stamped, Mapping):
        kind = str(stamped.get("kind") or "").strip() or "unknown"
        halt = bool(stamped.get("halt"))
        skip_text = _skip_phase_list(stamped.get("skip_phases"))
    else:
        route = derive_correction_route(triage)
        if route is None:
            return None
        kind = route.kind
        halt = route.halt
        skip_text = _skip_phase_list(route.skip_phases)

    if halt:
        reason = _compact_reason(triage)
        text = (
            f"Correction route: {kind} → halted before implement; {reason}"
        )
        return CorrectionRouteDisplay(text=text, halted=True, kind=kind)

    if skip_text:
        outcome = _final_acceptance_outcome(phases)
        text = (
            f"Correction route: {kind} → skipped {skip_text}; "
            f"final_acceptance={outcome}"
        )
        return CorrectionRouteDisplay(text=text, halted=False, kind=kind)

    text = f"Correction route: {kind} → full correction path"
    return CorrectionRouteDisplay(text=text, halted=False, kind=kind)
