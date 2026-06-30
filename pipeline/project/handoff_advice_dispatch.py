# SPDX-License-Identifier: Apache-2.0
"""Advisory sub-flow dispatch for a paused phase handoff (focused helper).

Extracted from ``pipeline.project.handoff_advice`` (Architecture Fitness Gate):
the orchestrator-facing glue that turns an ``AdviceActionRequest`` (the UI
pseudo-actions ``advice`` / ``retry_with_advice``) into either an ordinary
``HandoffDecisionInput`` for the EXISTING decide + resume path, or ``None`` to
return to the main menu. All the advisor primitives (context, invoke, parse,
safety) live in ``handoff_advice``; the artifact/provenance helpers live in
``handoff_advice_artifact``. This module only sequences them.

The advisor functions are referenced through the ``handoff_advice`` module
object (``_adv.invoke_advisor`` etc.) so tests can monkeypatch them on that
module. This module imports no ``pipeline.project.app`` and is the only home for
the advisory dispatch responsibility, keeping ``handoff.py`` to pure dispatch.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Any

from pipeline.project import handoff_advice as _adv
from pipeline.project.handoff_advice_artifact import (
    build_provenance_note,
    write_advice_artifact,
)


def _handle_advice_request(
    run: Any,
    signal: Any,
    request: Any,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> Any | None:
    """Drive the advisory sub-flow for an ``AdviceActionRequest``.

    Returns a ``HandoffDecisionInput`` to feed the orchestrator's EXISTING
    decide + resume path — ``action='retry_feedback'`` with a provenance note
    pointing at the actually-written advice artifact, or ``action='halt'`` — or
    ``None`` to return to the main menu (no decision written). All advisor
    errors (invocation exception, unparseable response) warn and return
    ``None`` so the prompt loop never dies. This function never writes a
    decision artifact itself; it only produces the input the existing SDK
    decide call consumes.
    """
    from core.observability.logging import warn
    from pipeline.control.handoff_banners import render_advice_summary
    from pipeline.control.handoff_prompt import (
        AdviceFollowup,
        HandoffDecisionInput,
        prompt_advice_followup,
        prompt_confirm,
    )

    out = stdout or sys.stdout
    run_dir = getattr(run, "output_dir", None)
    if run_dir is None:
        warn("Advice unavailable: run has no output directory; returning to menu.")
        return None

    ctx = _adv.build_advice_context(run, signal)
    try:
        result = _adv.invoke_advisor(run, ctx)
    except Exception as exc:  # advisor invocation must never break the loop
        warn(f"Advisor invocation failed ({exc}); returning to menu.")
        return None
    advice = result.advice
    if "advice_unparseable" in advice.parse_warnings:
        warn("Advisor response could not be parsed; returning to menu.")
        return None

    # The advice object is the only durable write here — never a decision.
    relpath = write_advice_artifact(
        run_dir, signal.handoff_id, advice, ctx, usage=result.usage,
    )
    available = tuple(getattr(signal, "available_actions", ()) or ())

    if getattr(request, "kind", "") == "retry_with_advice":
        if advice.recommended_action != "retry_feedback":
            print(render_advice_summary(
                recommended_action=advice.recommended_action,
                confidence=advice.confidence, rationale=advice.rationale,
                retry_feedback_preview=advice.retry_feedback, risks=advice.risks,
                expected_files=advice.expected_files,
                operator_note=advice.operator_note,
            ), file=out)
            warn(
                f"Advisor recommended {advice.recommended_action!r}, not a "
                "retry; no automatic retry performed."
            )
            return None
        if "retry_feedback" not in available:  # defensive: menu shouldn't show
            warn("retry_feedback is not available for this handoff; cannot retry.")
            return None
        if advice.confidence == "low" and prompt_confirm(
            "Advisor confidence is low — generate a retry from this advice?",
            stdin=stdin, stdout=stdout,
        ) is not True:
            return None
        return HandoffDecisionInput(
            action="retry_feedback", feedback=advice.retry_feedback,
            note=build_provenance_note(relpath),
        )

    # kind == 'advice': interactive follow-up sub-menu (renders the summary).
    followup = prompt_advice_followup(
        recommended_action=advice.recommended_action,
        confidence=advice.confidence, rationale=advice.rationale,
        retry_feedback_preview=advice.retry_feedback, risks=advice.risks,
        expected_files=advice.expected_files, operator_note=advice.operator_note,
        stdin=stdin, stdout=stdout,
    )
    if not isinstance(followup, AdviceFollowup):
        return None  # aborted → back to menu
    if followup.action == "back":
        return None
    if followup.action == "halt":
        return HandoffDecisionInput(
            action="halt", note=build_provenance_note(relpath),
        )

    # apply — ONLY a retry_feedback recommendation can become a durable retry.
    # A non-retry recommendation (continue / halt / continue_with_waiver) is
    # render-only even when the operator picks "apply": the compact summary was
    # already shown by the follow-up prompt, so return to the menu without
    # recording any decision. Gating on the recommendation here guarantees a
    # low-confidence confirmation can never upgrade a non-retry recommendation
    # into a retry_feedback decision.
    if advice.recommended_action != "retry_feedback":
        warn(
            f"Advisor recommended {advice.recommended_action!r}, not a retry; "
            "no decision recorded. Returning to menu."
        )
        return None
    # For a retry_feedback recommendation ``auto_apply_ok`` is False only when
    # confidence is low; require an explicit operator confirmation in that case.
    safety = _adv.classify_advice_safety(advice)
    if safety.needs_confirmation and prompt_confirm(
        "Advisor confidence is low — apply this retry anyway?",
        stdin=stdin, stdout=stdout,
    ) is not True:
        return None

    if followup.feedback is not None:
        # Operator edited the feedback. The note must point at an advice
        # artifact whose retry_feedback IS the applied text, so persist a
        # divergent advice version and use ITS path — never the original's.
        relpath = write_advice_artifact(
            run_dir, signal.handoff_id,
            replace(advice, retry_feedback=followup.feedback), ctx,
            usage=result.usage,
        )
        feedback = followup.feedback
    else:
        feedback = advice.retry_feedback
    return HandoffDecisionInput(
        action="retry_feedback", feedback=feedback,
        note=build_provenance_note(relpath),
    )


__all__ = ["_handle_advice_request"]
