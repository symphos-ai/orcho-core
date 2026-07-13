# SPDX-License-Identifier: Apache-2.0
"""Non-interactive (CI) advisory sub-flow for a paused phase handoff.

Modelled on :mod:`pipeline.project.handoff_advice_dispatch`, but **policy-driven
and prompt-free**: where the interactive dispatch reads operator menus, this
sub-flow turns an eligible paused rejected/incomplete handoff into either an
ordinary ``HandoffDecisionInput(action='retry_feedback')`` carrying ``ci_agent``
provenance — for the SAME decide + resume path a human ``retry_feedback`` uses —
or a typed stop (``needs_operator`` / ``halt`` / ``budget_exhausted`` /
``repeated_finding``). No prompts, no confirmations, no follow-up menu.

The advisor primitives are referenced through the ``handoff_advice`` module
object (``_adv.invoke_advisor`` etc.) so tests can monkeypatch them; the budget
and safety gates live in ``handoff_advice_policy``. The only durable write is the
advice artifact — the decision itself flows through the existing SDK path. This
module imports no ``pipeline.project.app``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pipeline.project import handoff_advice as _adv
from pipeline.project.handoff_advice_artifact import (
    build_provenance_note,
    write_advice_artifact,
)
from pipeline.project.handoff_advice_policy import (
    build_scope,
    evaluate_ci_gates,
    findings_fingerprint,
)

if TYPE_CHECKING:
    from pipeline.control.handoff_prompt import HandoffDecisionInput
    from pipeline.project.handoff_advice_policy import HandoffAdvicePolicy

_Fingerprint = frozenset


@dataclass(frozen=True, slots=True)
class CiAdviceOutcome:
    """Result of :func:`handle_ci_advice`.

    ``outcome`` is ``'retry'`` (``decision_input`` set, ready for the existing
    decide + resume path) or ``'stop'`` (``state``/``reason`` describe why). The
    advisor-derived fields (``last_recommendation`` / ``last_confidence`` /
    ``findings_fingerprint`` / ``scope_unchecked``) are ALWAYS populated for the
    ``_ci_agent_advice`` aggregate — empty defaults for a pre-advisor stop.
    """

    outcome: str
    decision_input: HandoffDecisionInput | None = None
    state: str = ""
    reason: str = ""
    last_recommendation: str = ""
    last_confidence: str = ""
    findings_fingerprint: frozenset[tuple[str, str, str]] = field(
        default_factory=_Fingerprint,
    )
    scope_unchecked: bool = False


def _stop(state: str, reason: str, **fields: Any) -> CiAdviceOutcome:
    """Build a typed stop outcome (no decision input)."""
    return CiAdviceOutcome(outcome="stop", state=state, reason=reason, **fields)


def handle_ci_advice(
    run: Any,
    signal: Any,
    policy: HandoffAdvicePolicy,
    *,
    budget_remaining: int,
    prev_findings_fingerprint: frozenset[tuple[str, str, str]] | None = None,
) -> CiAdviceOutcome:
    """Drive the prompt-free CI advisory sub-flow for a paused ``signal``.

    Sequence: (1) ineligible (policy not auto, or ``retry_feedback`` not offered)
    → stop; (2) exhausted budget → stop ``budget_exhausted``; (3) invoke the
    read-only advisor (advisor exception / unparseable response → stop
    ``needs_operator``); (4) persist the advice artifact (the only durable
    write); (5) apply :func:`evaluate_ci_gates` (it owns the destructive + scope
    gates) with a repeated-finding flag derived from the fingerprint; (6)
    proceed → a ``ci_agent`` ``retry_feedback`` decision input, else a typed stop.
    """
    available = tuple(getattr(signal, "available_actions", ()) or ())
    if _adv.hygiene_gate_advice(signal) is not None:
        return _stop("needs_operator", "waiver")
    if not policy.auto_retry_with_agent:
        return _stop("needs_operator", "policy_no_auto_retry")
    if "retry_feedback" not in available:
        return _stop("needs_operator", "retry_feedback_unavailable")
    if budget_remaining <= 0:
        return _stop("budget_exhausted", "budget_exhausted")

    run_dir = getattr(run, "output_dir", None)
    if run_dir is None:
        return _stop("needs_operator", "no_output_dir")

    ctx = _adv.build_advice_context(run, signal)
    try:
        result = _adv.invoke_advisor(run, ctx)
    except Exception:  # advisor invocation must never crash the CI run
        return _stop("needs_operator", "advisor_error")

    advice = result.advice
    findings = ctx.findings
    fingerprint = findings_fingerprint(findings)
    advisor_fields = {
        "last_recommendation": advice.recommended_action,
        "last_confidence": advice.confidence,
        "findings_fingerprint": fingerprint,
    }
    if "advice_unparseable" in advice.parse_warnings:
        return _stop("needs_operator", "advice_unparseable", **advisor_fields)

    # The advice object is the ONLY durable write here — never a decision.
    relpath = write_advice_artifact(
        run_dir,
        signal.handoff_id,
        advice,
        ctx,
        usage=result.usage,
    )

    scope = build_scope(getattr(run, "state", None))
    repeated = prev_findings_fingerprint is not None and fingerprint == prev_findings_fingerprint
    safety = _adv.classify_advice_safety(advice, findings)
    decision = evaluate_ci_gates(
        advice,
        safety,
        findings,
        scope,
        budget_remaining,
        repeated,
    )
    advisor_fields["scope_unchecked"] = decision.scope_unchecked

    if decision.proceed:
        from pipeline.control.handoff_prompt import HandoffDecisionInput

        decision_input = HandoffDecisionInput(
            action="retry_feedback",
            feedback=advice.retry_feedback,
            note=build_provenance_note(relpath, source="ci_agent"),
        )
        return CiAdviceOutcome(
            outcome="retry",
            decision_input=decision_input,
            **advisor_fields,
        )
    return _stop(decision.stop_state, decision.reason, **advisor_fields)


__all__ = ["CiAdviceOutcome", "handle_ci_advice"]
