"""pipeline/cross_project/gate_entries.py — skipped-gate audit entries.

Builders for the per-alias ``contract_check`` entry and the singleton
``cross_final_acceptance`` entry written into ``session["phases"]`` when
a runner-owned cross gate is skipped. The shape preserves the same keys
consumers expect from the executed-gate path so dashboards / MCP /
evidence don't need separate code paths to render skipped state.

Skipped entries never set ``approved=true``. The ``skipped`` flag plus
``skip_reason`` / ``source`` carry the audit trail; ``on_skip`` carries
forward the profile policy so later precondition checks can decide
whether the skip blocks system release.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.runtime import CrossGateSkipPolicy


def child_readiness_contract_entry(
    *,
    alias: str,
    child_status: str,
    child_reason: str,
) -> dict[str, Any]:
    """Build an unevaluable contract entry for a non-ready child.

    This is a readiness precondition, not a policy skip or a compatibility
    verdict.  It deliberately has no ``skipped`` / ``on_skip`` fields: policy
    skip semantics must never decide what happens to a child that did not
    produce a reviewable terminal-success session.
    """
    return {
        "approved":      False,
        "verdict":       "NOT_EVALUABLE",
        "not_evaluable": True,
        "source":        "precondition",
        "reason":        "child_readiness",
        "child_status":  child_status,
        "child_reason":  child_reason,
        "short_summary": (
            f"contract_check not evaluable for [{alias}]: child is "
            f"{child_status} ({child_reason})."
        ),
        "findings":      [],
        "risks":         [],
        "checks":        [],
    }


def child_readiness_blocking_aliases(
    entries: Mapping[str, Any] | Any,
) -> tuple[str, ...]:
    """Return aliases carrying the terminal child-readiness precondition.

    This deliberately recognises the full NOT_EVALUABLE precondition shape,
    rather than treating arbitrary contract-check failures as readiness
    failures.  Both delivery admission and terminal finalization consume this
    predicate so a policy-skipped CFA cannot green-light a non-success child.
    """
    if not isinstance(entries, Mapping):
        return ()
    return tuple(
        alias
        for alias, entry in entries.items()
        if isinstance(alias, str)
        and isinstance(entry, Mapping)
        and entry.get("verdict") == "NOT_EVALUABLE"
        and entry.get("not_evaluable") is True
        and entry.get("source") == "precondition"
        and entry.get("reason") == "child_readiness"
    )


def skipped_contract_entry(
    *,
    alias: str,
    reason: str,
    source: str,
    on_skip: CrossGateSkipPolicy,
    operator_feedback: str = "",
) -> dict[str, Any]:
    """Build a per-alias ``contract_check`` entry for a skipped gate.

    ``reason`` / ``source`` map to the documented combinations:

    - operator skip:  ``operator_decision`` / ``operator``
    - policy never:   ``policy_never``      / ``policy``
    - policy disabled:``policy_disabled``   / ``policy``
    """
    entry: dict[str, Any] = {
        "approved":      False,
        "verdict":       "SKIPPED",
        "skipped":       True,
        "skip_reason":   reason,
        "on_skip":       on_skip.value,
        "source":        source,
        "short_summary": _short_summary_contract(alias, reason, source),
        "findings":      [],
        "risks":         [],
        "checks":        [],
    }
    if operator_feedback:
        entry["operator_feedback"] = operator_feedback
    return entry


def skipped_release_entry(
    *,
    reason: str,
    source: str,
) -> dict[str, Any]:
    """Build the singleton ``cross_final_acceptance`` entry for a gate
    skipped by profile policy.

    Only emitted when the gate is disabled / ``run=never``. The shape
    mirrors :class:`pipeline.session_adapters.FinalAcceptanceAdapter`
    dual-shape output so consumers (Web/MCP/evidence) can read the same
    fields whether the gate ran or was skipped.
    """
    return {
        "approved":          False,
        "verdict":           "SKIPPED",
        "ship_ready":        False,
        "skipped":           True,
        "skip_reason":       reason,
        "source":            source,
        "short_summary":     _short_summary_release(reason, source),
        "release_blockers":  [],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "not_applicable",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "not_applicable",
        },
    }


def _short_summary_contract(alias: str, reason: str, source: str) -> str:
    if source == "operator":
        return f"contract_check skipped by operator for [{alias}]."
    if reason == "policy_disabled":
        return (
            f"contract_check skipped by profile policy (disabled) "
            f"for [{alias}]."
        )
    if reason == "policy_never":
        return (
            f"contract_check skipped by profile policy (run=never) "
            f"for [{alias}]."
        )
    return f"contract_check skipped for [{alias}]."


def _short_summary_release(reason: str, source: str) -> str:
    if reason == "policy_disabled":
        return "cross_final_acceptance skipped by profile policy."
    if reason == "policy_never":
        return (
            "cross_final_acceptance skipped by profile policy (run=never)."
        )
    return f"cross_final_acceptance skipped ({source}: {reason})."


__all__ = [
    "child_readiness_blocking_aliases",
    "child_readiness_contract_entry",
    "skipped_contract_entry",
    "skipped_release_entry",
]
