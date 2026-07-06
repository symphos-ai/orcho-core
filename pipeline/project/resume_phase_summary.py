# SPDX-License-Identifier: Apache-2.0
"""Read-only per-phase resume summary rendering.

On a resumed run, phases that already completed in a prior run are *skipped*
rather than re-executed. The operator has lost their live output, so this
module turns the durable ``session["phases"]`` record for such a phase into a
compact, operator-facing one-liner — the resume output then says *what each
phase produced*, not just that it was skipped.

It is a pure formatting layer — no I/O, no ``print``, and no imports from the
runtime, orchestrator, or checkpoint modules — mirroring the focused shape of
:mod:`pipeline.project.correction_route_display`. The render seam
(``pipeline.project.profile_dispatch.emit_phase_log_end``) delegates here and
decides *where* / *how* (terminal vs. log, color tone, the
``(skipped on resume)`` marker) to render the returned text.

:func:`format_resume_phase_summary` takes the whole ``session["phases"]``
mapping (like ``format_correction_route_summary``) and returns either the
summary text (without the ``↳`` prefix or the marker) or ``None`` to signal
"fall back to the generic skip reason". It never raises and never returns an
empty string.

Persisted shapes it reads (see :mod:`pipeline.session_adapters`):

- ``plan`` — a *list* of per-round attempts; the last mapping carries
  ``total_atomic_tasks`` and ``parsed_file_paths``.
- ``validate_plan`` — a *list* of per-attempt records; the last mapping carries
  ``approved`` plus optional ``short_summary`` / ``verdict``.
- ``implement`` — a single dict with ``progress {completed, total}`` and an
  optional ``test_result {skipped, passed, ...}`` (absent when no test gate ran).
- ``review_changes`` / ``repair_changes`` — folded into the ``rounds`` *list*;
  each round carries ``round``, ``critique`` and an optional ``test_result``.
  No numeric per-round findings count is persisted, so a non-empty critique is
  rendered as findings *raised* (presence), not a fabricated count.
- ``final_acceptance`` — a single dict with ``verdict`` / ``ship_ready`` /
  ``release_blockers``.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

#: Max width of a free-form reason fragment (a validate_plan rejection summary)
#: before it is shortened — keeps the resume line to a single terminal row,
#: mirroring ``correction_route_display._REASON_WIDTH`` intent.
_REASON_WIDTH = 100


def _last_mapping(value: Any) -> Mapping[str, Any] | None:
    """Last mapping record from a persisted per-attempt list (or a lone dict).

    ``plan`` / ``validate_plan`` persist as a *list* of attempts
    (``session['phases']['plan'][]``); ``implement`` / ``final_acceptance`` as a
    single dict. Returns the last mapping entry for a list, the mapping itself
    for a dict, or ``None`` for an empty / mapping-less / wrong-typed value.
    """
    if isinstance(value, Mapping):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        attempts = [item for item in value if isinstance(item, Mapping)]
        return attempts[-1] if attempts else None
    return None


def _test_status(result: Any) -> str:
    """``pass`` | ``fail`` | ``n/a`` from a persisted ``test_result`` dict.

    The dict shape is ``{skipped, passed, output, duration}`` and is *absent*
    when no test gate ran (→ ``n/a``). A skipped result is also ``n/a``.
    """
    if not isinstance(result, Mapping):
        return "n/a"
    if result.get("skipped"):
        return "n/a"
    return "pass" if result.get("passed") else "fail"


def _plan_summary(phases: Mapping[str, Any]) -> str | None:
    entry = _last_mapping(phases.get("plan"))
    if entry is None:
        return None
    total = entry.get("total_atomic_tasks")
    paths = entry.get("parsed_file_paths")
    if not isinstance(total, int) or not isinstance(paths, (list, tuple)):
        return None
    return f"{total} atomic tasks, {len(paths)} artifacts"


def _validate_plan_summary(phases: Mapping[str, Any]) -> str | None:
    entry = _last_mapping(phases.get("validate_plan"))
    if entry is None or "approved" not in entry:
        return None
    if entry.get("approved"):
        return "approved"
    reason = str(entry.get("short_summary") or entry.get("verdict") or "").strip()
    if not reason:
        return "REJECTED"
    return f"REJECTED — {textwrap.shorten(reason, width=_REASON_WIDTH, placeholder='…')}"


def _implement_summary(phases: Mapping[str, Any]) -> str | None:
    entry = phases.get("implement")
    if not isinstance(entry, Mapping):
        return None
    progress = entry.get("progress")
    if not isinstance(progress, Mapping):
        return None
    completed = progress.get("completed")
    total = progress.get("total")
    if not isinstance(completed, int) or not isinstance(total, int):
        return None
    return f"{completed}/{total} subtasks, tests {_test_status(entry.get('test_result'))}"


def _rounds_summary(phases: Mapping[str, Any]) -> str | None:
    rounds = phases.get("rounds")
    if not isinstance(rounds, Sequence) or isinstance(rounds, (str, bytes)):
        return None
    entries = [r for r in rounds if isinstance(r, Mapping)]
    if not entries:
        return None
    last = entries[-1]
    r = last.get("round")
    if not isinstance(r, int):
        return None
    critique = str(last.get("critique") or "").strip()
    return f"round {r} — clean" if not critique else f"round {r} — findings raised"


def _final_acceptance_summary(phases: Mapping[str, Any]) -> str | None:
    entry = _last_mapping(phases.get("final_acceptance"))
    if entry is None:
        return None
    verdict = str(entry.get("verdict") or "").strip().upper()
    ship_ready = entry.get("ship_ready")
    if verdict == "APPROVED" or ship_ready is True:
        return "APPROVED, ship-ready"
    if verdict == "REJECTED" or ship_ready is False:
        blockers = entry.get("release_blockers")
        n = len(blockers) if isinstance(blockers, (list, tuple)) else 0
        return f"REJECTED — {n} blockers"
    return None


#: Phase name → per-phase summary builder. Phases absent from this table
#: (e.g. ``correction_triage``, ``compliance_check``) fall through to the
#: generic skip reason at the render seam.
_SUMMARY_BY_PHASE = {
    "plan":             _plan_summary,
    "validate_plan":    _validate_plan_summary,
    "implement":        _implement_summary,
    "review_changes":   _rounds_summary,
    "repair_changes":   _rounds_summary,
    "final_acceptance": _final_acceptance_summary,
}


def format_resume_phase_summary(
    name: str, phases: Mapping[str, Any] | None,
) -> str | None:
    """One-line resume summary for phase ``name`` from ``session['phases']``.

    Returns the summary text (no ``↳`` prefix, no ``(skipped on resume)``
    marker — the seam adds those) or ``None`` to signal "fall back to the
    generic skip reason". Best-effort: an unknown phase, a missing/partial
    result, or any unexpected shape yields ``None`` rather than a crash or an
    empty line.
    """
    if not isinstance(phases, Mapping):
        return None
    builder = _SUMMARY_BY_PHASE.get(name)
    if builder is None:
        return None
    try:
        text = builder(phases)
    except Exception:  # pragma: no cover — defensive: never crash the render
        return None
    if not text or not text.strip():
        return None
    return text
