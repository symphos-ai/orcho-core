# SPDX-License-Identifier: Apache-2.0
"""The machine-readable face of ``orcho status`` — one JSON object.

``status_to_json`` projects the exact same resolved state the text report
renders: the ``RunStatus`` snapshot, the single ``RunDiagnosis`` the facade
asked core for, and the already-rendered ``Next:`` lines. It re-derives
nothing — no second ``run_diagnosis`` call, no re-classification of a run —
so the JSON and the text can never disagree.

The shape is fixed: every documented key is always present, ``null`` (or an
empty list) standing in for absence. Nested typed values go through
``sdk.to_jsonable``, the same projection MCP embeds, so tuples become lists
and ``Path`` becomes ``str``. Nothing here paints: the result is data, not
terminal output.
"""
from __future__ import annotations

from typing import Any

from sdk import to_jsonable


def status_to_json(
    status: Any,
    *,
    diagnosis: Any | None,
    diagnosis_error: str | None,
    stalled_reason: str | None,
    next_lines: list[str],
) -> dict[str, Any]:
    """Project a resolved ``orcho status`` invocation into a JSON object.

    ``next_lines`` are passed through verbatim as ``next_step.lines`` — the
    text after the ``Next:`` label, empty for an ``active`` run and a single
    ``diagnosis unavailable`` line when the diagnosis failed. The remaining
    ``next_step`` fields read only the typed diagnosis and are ``null`` /
    ``[]`` when there is no diagnosis to read.
    """
    return {
        "run_id": status.run_ref.run_id,
        "run_dir": str(status.run_ref.run_dir),
        "status": to_jsonable(status),
        "stalled_reason": stalled_reason,
        "diagnosis": to_jsonable(diagnosis) if diagnosis is not None else None,
        "diagnosis_error": diagnosis_error,
        "next_step": _next_step(diagnosis, next_lines),
    }


def _next_step(diagnosis: Any | None, next_lines: list[str]) -> dict[str, Any]:
    """The structured twin of the text ``Next:`` block."""
    if diagnosis is None:
        return {
            "condition": None,
            "action": None,
            "run_id": None,
            "available_actions": [],
            "handoff_id": None,
            "lines": list(next_lines),
        }
    return {
        "condition": diagnosis.condition,
        "action": diagnosis.recommended_next_action,
        "run_id": diagnosis.recommended_run_id or diagnosis.run_id,
        "available_actions": list(diagnosis.available_actions),
        "handoff_id": diagnosis.handoff_id,
        "lines": list(next_lines),
    }
