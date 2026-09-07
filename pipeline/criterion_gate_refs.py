# SPDX-License-Identifier: Apache-2.0
"""pipeline.criterion_gate_refs — resolve criterion gate refs to official gates.

An ``executable`` criterion may optionally name complete scheduled identities
``(command, hook, phase)``. Before implement starts, every one of them must
resolve against the run's durable scheduled-gate ledger — the same identity set
the engine actually runs, written at run setup by
:mod:`pipeline.project.verification_ledger_runtime` before any phase executes.

The resolution is **fail-closed**:

* no ledger, or an unreadable one, rejects every explicit gate reference. A
  project that declares no verification contract has no official gates, so a
  criterion claiming gate proof there cannot be honoured;
* an identity the ledger does not declare is rejected;
* an identity the run has already *decided against* (``selected is False``) is
  rejected.

``selected is None`` is deliberately **not** an error here, and that is the one
place this module is permissive on purpose. At plan time the ledger holds the
declaration snapshot: the selection epoch for a hook has not resolved yet, and
it cannot be forced early because path- and task-kind-based selection rules
depend on the implement diff, which does not exist during planning. Resolving
selection at plan time would freeze it against an empty change set and silently
change which gates a run picks. Such a ref stays fail-closed downstream
instead: :func:`pipeline.criterion_matrix.gate_state_from_disposition` maps an
identity the run never selected onto ``not_selected``, and a non-``proven``
executable row blocks readiness.

This module resolves; it never decides policy, freshness, or consequence.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.contracts.criteria import AcceptanceCriterion

__all__ = [
    "CriterionGateRefError",
    "OfficialGateIdentities",
    "official_gate_identities",
    "plan_gate_ref_problems",
    "render_gate_ref_rejection",
    "unprovable_implied_criteria",
    "unresolved_gate_refs",
    "validate_criterion_gate_refs",
    "validate_plan_gate_refs",
]


class CriterionGateRefError(ValueError):
    """Raised when a criterion's gate refs do not resolve to official gates."""


@dataclass(frozen=True, slots=True)
class OfficialGateIdentities:
    """The ledger's declared identities, split by resolved selection state.

    ``pending`` holds identities whose selection epoch has not resolved yet
    (``selected is None``) — declared, not yet decided either way.
    """

    declared: frozenset[tuple[str, str, str]]
    selected: frozenset[tuple[str, str, str]]
    rejected: frozenset[tuple[str, str, str]]
    pending: frozenset[tuple[str, str, str]]


def official_gate_identities(run_dir: Path | str) -> OfficialGateIdentities:
    """Read the durable ledger's identities.

    Raises :class:`CriterionGateRefError` when the ledger is absent or
    unreadable: an unresolvable authority must never read as "nothing to
    check".
    """
    from pipeline.verification_ledger_store import ledger_path, load_ledger

    run_dir = Path(run_dir)
    path = ledger_path(run_dir)
    if not path.exists():
        raise CriterionGateRefError(
            f"no scheduled-gate ledger at {path}; this run declares no "
            "official gates, so an executable criterion has nothing to resolve "
            "against"
        )
    try:
        ledger = load_ledger(run_dir)
    except Exception as e:  # noqa: BLE001 — any unreadable ledger is fatal here
        raise CriterionGateRefError(
            f"scheduled-gate ledger at {path} is unreadable: {e}"
        ) from e

    declared, selected, rejected, pending = set(), set(), set(), set()
    for row in ledger.rows:
        declared.add(row.identity)
        if row.selected is True:
            selected.add(row.identity)
        elif row.selected is False:
            rejected.add(row.identity)
        else:
            pending.add(row.identity)
    return OfficialGateIdentities(
        declared=frozenset(declared),
        selected=frozenset(selected),
        rejected=frozenset(rejected),
        pending=frozenset(pending),
    )


def unresolved_gate_refs(
    criteria: Sequence[AcceptanceCriterion],
    identities: OfficialGateIdentities,
) -> list[str]:
    """One human-readable problem per unresolvable ref, in declaration order."""
    problems: list[str] = []
    for criterion in criteria:
        if criterion.verify != "executable":
            continue
        for ref in criterion.gate_refs:
            if ref.identity not in identities.declared:
                problems.append(
                    f"{criterion.id} references gate {ref.label()!r}, which the "
                    "project's verification contract does not declare"
                )
            elif ref.identity in identities.rejected:
                problems.append(
                    f"{criterion.id} references gate {ref.label()!r}, which this "
                    "run has resolved as not selected"
                )
    return problems


def unprovable_implied_criteria(
    criteria: Sequence[AcceptanceCriterion],
    identities: OfficialGateIdentities | None,
    *,
    cause: str | None = None,
) -> list[str]:
    """One problem per implied executable criterion no official gate can prove.

    An executable criterion with no ``gate_refs`` is bound by the engine to the
    run's *selected* gates (ADR 0188 addendum). That binding needs something to
    bind to: when the run declares no gate at all (no scheduled-gate ledger —
    the project has no verification contract, ``identities is None``) or every
    declared gate is already resolved as not selected, the criterion can never
    reach ``proven`` and the run is doomed to an engine-backstop REJECT at final
    acceptance, after the whole implement / review budget was spent. Diagnosing
    it at plan review costs one planning round instead (ADR 0191, E2).
    ``cause`` names why the ledger could not be read, when that is the reason.
    """
    bindable: frozenset[tuple[str, str, str]] = (
        identities.declared - identities.rejected if identities is not None
        else frozenset()
    )
    if bindable:
        return []
    if identities is None:
        why = (
            "this run declares no official verification gate to bind it to"
            + (f" ({cause})" if cause else "")
        )
    else:
        why = (
            "every gate this run declares is already resolved as not selected, "
            "so there is nothing to bind it to"
        )
    return [
        f"{criterion.id} is executable but names no gate, and {why}; it could "
        "never be proven. Reclassify it as agent_assertion (inspected by the "
        "reviewer) or human (decided by an operator), or have the project "
        "declare the check it relies on as a verification gate"
        for criterion in criteria
        if criterion.verify == "executable" and not criterion.gate_refs
    ]


def _gate_ref_problems(
    criteria: Sequence[AcceptanceCriterion], run_dir: Path | str | None,
) -> list[str]:
    """The single rule behind the raising and the data-returning twins."""
    executable = [c for c in criteria if c.verify == "executable"]
    if not executable:
        return []
    explicit = [c for c in executable if c.gate_refs]
    implied = [c for c in executable if not c.gate_refs]
    if run_dir is None:
        # No run directory means no ledger can exist yet (a plan-only context,
        # not a run): explicit refs are unresolvable, implied ones are left to
        # the run that will own them (ADR 0188 addendum).
        if explicit:
            return [
                "this run has no output directory, so its scheduled-gate ledger "
                "cannot be read; an executable criterion cannot be resolved"
            ]
        return []
    try:
        identities = official_gate_identities(run_dir)
    except CriterionGateRefError as e:
        problems = [str(e)] if explicit else []
        # An absent ledger on a real run means the project declares no gates at
        # all: an implied criterion has nothing to bind to, ever.
        return problems + unprovable_implied_criteria(implied, None, cause=str(e))
    return (
        unresolved_gate_refs(explicit, identities)
        + unprovable_implied_criteria(implied, identities)
    )


def validate_criterion_gate_refs(
    criteria: Sequence[AcceptanceCriterion], run_dir: Path | str | None,
) -> None:
    """Raise :class:`CriterionGateRefError` when any executable criterion cannot resolve.

    A plan with no explicit gate refs needs no ledger in a plan-only context
    (``run_dir is None``) and is accepted without touching one; on a real run an
    implied executable criterion still needs at least one declared, selectable
    gate to bind to (see :func:`unprovable_implied_criteria`).
    """
    problems = _gate_ref_problems(criteria, run_dir)
    if problems:
        raise CriterionGateRefError("; ".join(problems))


def validate_plan_gate_refs(plan: Any, run_dir: Path | str | None) -> None:
    """Convenience wrapper over a :class:`~pipeline.plan_parser.ParsedPlan`."""
    validate_criterion_gate_refs(getattr(plan, "acceptance_criteria", ()), run_dir)


def plan_gate_ref_problems(
    plan: Any, run_dir: Path | str | None,
) -> list[str]:
    """Return one problem per unresolvable / unprovable criterion, without raising.

    The raising twin (:func:`validate_plan_gate_refs`) states the same
    fail-closed rule as an exception. Plan review needs the rule as *data*: an
    unresolvable ref — or an implied executable criterion the run has no gate
    to bind to — is a fixable mistake in the plan's own text, so it is routed
    to the planner as a rejection verdict and costs one more round rather than
    ending the run. Every fail-closed condition of the raising path is
    preserved here as a problem string, including a missing or unreadable
    ledger.
    """
    criteria = getattr(plan, "acceptance_criteria", ()) or ()
    return _gate_ref_problems(criteria, run_dir)


def _identity_label(identity: tuple[str, str, str]) -> str:
    command, hook, phase = identity
    return f"{command} @ {hook}{f' {phase}' if phase else ''}"


def render_gate_ref_rejection(
    problems: Sequence[str], declared: frozenset[tuple[str, str, str]],
) -> str:
    """Render a valid validate-plan review for unresolvable gate refs.

    Mirrors :func:`pipeline.verification_ownership.render_verification_ownership_rejection`:
    the engine detected the violation deterministically, so the verdict is
    synthesized instead of spending a reviewer call on a question already
    answered. The declared identities travel with the finding because the
    planner cannot fix the ref without them.
    """
    import json

    catalogue = ", ".join(sorted(_identity_label(i) for i in declared)) or "none"
    if declared:
        required_fix = (
            "Omit gate_refs to use engine binding, or correct each explicit "
            f"ref to a declared identity: {catalogue}. Keep (command, hook, phase) "
            "triple complete, and use the gate's name as the command."
        )
    else:
        required_fix = (
            "This run declares no verification gate, so no criterion can be "
            "'executable' here. Reclassify each such criterion as "
            "agent_assertion (the reviewer inspects it) or human (an operator "
            "decides it), and state the exact command in its intent so the "
            "implementer still runs it; alternatively the project must declare "
            "that command as a verification gate before the run starts."
        )
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": (
            "The plan's executable acceptance criteria do not resolve to "
            "verification gates the project declares."
        ),
        "findings": [{
            "id": "criterion-gate-refs",
            "severity": "P1",
            "title": "Acceptance criterion cannot be proven by a declared gate",
            "body": (
                "; ".join(problems)
                + ". A gate_ref names a scheduled gate by its declared "
                "name, not by the shell command that gate runs; an executable "
                "criterion without gate_refs is bound to the run's selected gates "
                "and needs at least one to exist."
            ),
            "required_fix": required_fix,
        }],
        "risks": [],
        "checks": [
            "Resolved each executable criterion's gate_refs (or engine binding) "
            "against the run's durable scheduled-gate ledger."
        ],
    })
