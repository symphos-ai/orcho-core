# SPDX-License-Identifier: Apache-2.0
"""pipeline.criterion_gate_refs — resolve criterion gate refs to official gates.

An ``executable`` criterion names complete scheduled identities
``(command, hook, phase)``. Before implement starts, every one of them must
resolve against the run's durable scheduled-gate ledger — the same identity set
the engine actually runs, written at run setup by
:mod:`pipeline.project.verification_ledger_runtime` before any phase executes.

The resolution is **fail-closed**:

* no ledger, or an unreadable one, rejects every executable criterion. A
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


def validate_criterion_gate_refs(
    criteria: Sequence[AcceptanceCriterion], run_dir: Path | str | None,
) -> None:
    """Raise :class:`CriterionGateRefError` when any ref fails to resolve.

    A plan with no executable criterion needs no ledger at all and is accepted
    without touching one.
    """
    executable = [c for c in criteria if c.verify == "executable"]
    if not executable:
        return
    if run_dir is None:
        raise CriterionGateRefError(
            "this run has no output directory, so its scheduled-gate ledger "
            "cannot be read; an executable criterion cannot be resolved"
        )
    problems = unresolved_gate_refs(executable, official_gate_identities(run_dir))
    if problems:
        raise CriterionGateRefError("; ".join(problems))


def validate_plan_gate_refs(plan: Any, run_dir: Path | str | None) -> None:
    """Convenience wrapper over a :class:`~pipeline.plan_parser.ParsedPlan`."""
    validate_criterion_gate_refs(getattr(plan, "acceptance_criteria", ()), run_dir)
