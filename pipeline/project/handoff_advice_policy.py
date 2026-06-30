# SPDX-License-Identifier: Apache-2.0
"""CI handoff-advice policy: mode resolution, budget, and safety gates (pure).

When a phase handoff pauses on a rejected/incomplete verdict in a
non-interactive (CI) run, the orchestrator may turn the advisor's recommendation
into an automatic ``retry_feedback`` decision — but only behind explicit budget
and safety gates. This module owns those gates as a focused, **pure** policy
surface:

* :class:`HandoffAdvicePolicy` — the immutable, test-overridable policy object
  (auto-retry switch, explicit ``max_agent_retries`` budget, the
  ``require_human_for`` reason list).
* :func:`resolve_handoff_advice_policy` — derive the policy from a run's
  ``no_interactive`` mode.
* :func:`build_scope` — the write-scope globs a retry may touch, drawn from
  ``state.parsed_plan`` (plan + subtask ``owned_files``/``allowed_modifications``).
* :func:`is_destructive_recommendation` — an auditable classifier over an
  explicit marker list, mirroring ``_has_blocking_severity``'s safe default.
* :func:`evaluate_ci_gates` — the single ``proceed | stop(reason, state)``
  decision combining every gate.
* :func:`findings_fingerprint` — the id/severity/title fingerprint used to
  detect a repeated identical finding across an agent retry.

It calls no provider and writes no decision artifact — it only decides whether a
CI auto-retry is *allowed*. The actual decide + resume flows through the same
SDK path a human ``retry_feedback`` uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

# ``_has_blocking_severity`` is the shared P>=3-is-safe predicate; reusing it
# keeps the repeated-finding gate consistent with the advisor safety classifier.
from pipeline.project.handoff_advice import _has_blocking_severity

if TYPE_CHECKING:
    from pipeline.project.handoff_advice import AdviceSafety, HandoffAdvice

#: Explicit, auditable destructive markers searched (case-insensitive) across an
#: advice's free-text fields. Kept as a module constant so the gate stays
#: reviewable and extensible; matching is positive-recognition only (mirrors
#: ``_has_blocking_severity``'s safe default: unrecognised text is NOT
#: destructive, so the gate never invents false stops).
_DESTRUCTIVE_MARKERS: tuple[str, ...] = (
    "rm -rf",
    "git reset --hard",
    "git checkout -- ",
    "git restore",
    "git clean",
    "git push --force",
    "push -f",
    "force push",
    "drop table",
    "truncate table",
    "delete from",
    "history rewrite",
    "rebase --",
    "reflog expire",
    "wipe",
    "destroy",
)

#: Stop ``state`` values surfaced to the orchestrator. ``needs_operator`` parks
#: the handoff for a human; ``halt`` flows to HALTED finalization;
#: ``budget_exhausted`` / ``repeated_finding`` are compact bounded-loop stops.
_STATE_NEEDS_OPERATOR = "needs_operator"
_STATE_HALT = "halt"
_STATE_BUDGET_EXHAUSTED = "budget_exhausted"
_STATE_REPEATED_FINDING = "repeated_finding"


@dataclass(frozen=True, slots=True)
class HandoffAdvicePolicy:
    """Immutable CI handoff-advice policy.

    ``auto_retry_with_agent`` gates whether a non-interactive run may auto-retry
    at all (always ``False`` for interactive/TTY runs). ``max_agent_retries`` is
    the explicit, typed budget (safe default ``1``); tests override it to widen
    the budget gate. ``require_human_for`` is the auditable list of reasons that
    always park a handoff for a human rather than auto-retrying.
    """

    auto_retry_with_agent: bool = False
    max_agent_retries: int = 1
    require_human_for: tuple[str, ...] = (
        "waiver",
        "scope_change",
        "destructive_action",
        "repeated_p1",
        "advice_confidence_low",
    )


@dataclass(frozen=True, slots=True)
class CiGateDecision:
    """Outcome of :func:`evaluate_ci_gates`.

    ``proceed`` True authorises a single CI ``retry_feedback`` round; otherwise
    ``reason`` names the gate that fired and ``stop_state`` is one of
    ``needs_operator`` / ``halt`` / ``budget_exhausted`` / ``repeated_finding``.
    ``scope_unchecked`` flags the unlimited-scope (no parsed_plan) proceed so the
    integration can record an audit note.
    """

    proceed: bool
    reason: str = ""
    stop_state: str = ""
    scope_unchecked: bool = False


def resolve_handoff_advice_policy(run_or_flag: Any) -> HandoffAdvicePolicy:
    """Resolve the policy from a run (or a raw ``no_interactive`` bool).

    Interactive runs get a no-auto-action policy (``auto_retry_with_agent``
    False); non-interactive/CI runs get ``auto_retry_with_agent=True`` with the
    default ``max_agent_retries=1`` budget. Mode is read from ``run.no_interactive``.
    """
    if isinstance(run_or_flag, bool):
        no_interactive = run_or_flag
    else:
        no_interactive = bool(getattr(run_or_flag, "no_interactive", False))
    return HandoffAdvicePolicy(auto_retry_with_agent=no_interactive)


def build_scope(state: Any) -> frozenset[str]:
    """Write-scope globs a CI retry may touch, from ``state.parsed_plan``.

    Unions ``owned_files`` + ``allowed_modifications`` at the plan level and
    across every subtask. Returns an **empty frozenset as the unlimited marker**
    when ``parsed_plan`` is ``None`` or declares no scope — the scope gate then
    proceeds with ``scope_unchecked`` rather than blocking.
    """
    plan = getattr(state, "parsed_plan", None)
    if plan is None:
        return frozenset()
    globs: set[str] = set()
    globs.update(getattr(plan, "owned_files", ()) or ())
    globs.update(getattr(plan, "allowed_modifications", ()) or ())
    for subtask in getattr(plan, "subtasks", ()) or ():
        globs.update(getattr(subtask, "owned_files", ()) or ())
        globs.update(getattr(subtask, "allowed_modifications", ()) or ())
    return frozenset(g for g in globs if g)


def is_destructive_recommendation(advice: HandoffAdvice) -> bool:
    """True only when a destructive marker is positively recognised in advice.

    Analyses ONLY the free-text fields ``retry_feedback``, ``risks`` (joined),
    and ``operator_note`` (``recommended_action`` carries no destructive signal —
    the enum cannot express one). Case-insensitive match against
    :data:`_DESTRUCTIVE_MARKERS`; any single hit → ``True``. Safe default on
    ambiguous/empty text: ``False`` (proceed) — the gate fires only on positive
    recognition, mirroring :func:`_has_blocking_severity`.
    """
    haystack = " ".join(
        (
            advice.retry_feedback or "",
            " ".join(advice.risks),
            advice.operator_note or "",
        )
    ).lower()
    if not haystack.strip():
        return False
    return any(marker in haystack for marker in _DESTRUCTIVE_MARKERS)


def findings_fingerprint(findings: Any) -> frozenset[tuple[str, str, str]]:
    """Fingerprint findings by ``(id, severity, title)`` for repeat detection.

    Two rounds with an identical set of ``(id, severity, title)`` triples compare
    equal (repeated finding); any change in id/severity/title diverges them.
    """
    out: set[tuple[str, str, str]] = set()
    if not isinstance(findings, (list, tuple)):
        return frozenset()
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        out.add(
            (
                str(item.get("id") or "").strip(),
                str(item.get("severity") or "").strip().upper(),
                str(item.get("title") or "").strip(),
            )
        )
    return frozenset(out)


def _expected_files_in_scope(
    expected_files: tuple[str, ...], scope: frozenset[str],
) -> bool:
    """True when every expected file matches at least one scope glob (fnmatch)."""
    return all(
        any(fnmatch(path, glob) for glob in scope)
        for path in expected_files
        if path
    )


def evaluate_ci_gates(
    advice: HandoffAdvice,
    safety: AdviceSafety,
    findings: Any,
    scope: frozenset[str],
    budget_remaining: int,
    repeated: bool,
) -> CiGateDecision:
    """Combine every CI gate into one ``proceed | stop(reason, state)`` decision.

    Proceeds ONLY when the recommendation is ``retry_feedback`` at non-``low``
    confidence, budget remains, it is not a waiver, the finding is not a repeated
    blocking finding, no destructive marker is recognised, and the expected files
    stay within ``scope``. Gate order: waiver → halt → other non-retry →
    low-confidence → budget → repeated → destructive → scope (destructive before
    scope per spec). An empty ``scope`` proceeds with ``scope_unchecked=True``.
    """
    action = advice.recommended_action
    if action == "continue_with_waiver":
        return CiGateDecision(False, "waiver", _STATE_NEEDS_OPERATOR)
    if action == "halt":
        return CiGateDecision(False, "halt", _STATE_HALT)
    if action != "retry_feedback":
        return CiGateDecision(False, action or "unknown_action", _STATE_NEEDS_OPERATOR)
    if safety.needs_confirmation or advice.confidence == "low":
        return CiGateDecision(False, "advice_confidence_low", _STATE_NEEDS_OPERATOR)
    if budget_remaining <= 0:
        return CiGateDecision(False, "budget_exhausted", _STATE_BUDGET_EXHAUSTED)
    if repeated and _has_blocking_severity(findings):
        return CiGateDecision(False, "repeated_finding", _STATE_REPEATED_FINDING)
    if is_destructive_recommendation(advice):
        return CiGateDecision(False, "destructive_action", _STATE_NEEDS_OPERATOR)
    if not scope:
        return CiGateDecision(True, scope_unchecked=True)
    if not _expected_files_in_scope(advice.expected_files, scope):
        return CiGateDecision(False, "out_of_scope", _STATE_NEEDS_OPERATOR)
    return CiGateDecision(True)


__all__ = [
    "CiGateDecision",
    "HandoffAdvicePolicy",
    "build_scope",
    "evaluate_ci_gates",
    "findings_fingerprint",
    "is_destructive_recommendation",
    "resolve_handoff_advice_policy",
]
