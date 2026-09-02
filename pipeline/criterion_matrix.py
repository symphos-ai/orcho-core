# SPDX-License-Identifier: Apache-2.0
"""pipeline.criterion_matrix — the one criterion evidence/readiness reducer.

ADR 0188 names this module the single owner of the criterion matrix. It is
**pure**: it consumes typed criteria, typed task references, canonical
per-identity gate classifications, typed criterion claims/findings, and typed
human decisions, and returns one deterministic row per criterion plus a
summary.

What it deliberately does *not* do:

* parse transcript prose, Markdown, command output, or finding text;
* recompute receipt freshness or gate selection — those stay owned by
  :mod:`pipeline.verification_readiness` / :mod:`pipeline.verification_ledger`,
  whose canonical classifications arrive here already decided;
* decide delivery policy — final readiness *consumes* :attr:`MatrixSummary.ready`.

Two orders live here and they are different axes (ADR 0188 §3):

* :data:`CRITERION_STATE_ORDER` — the single canonical **serialization** order
  for ``counts_by_state`` keys and every state enumeration in evidence JSON,
  the SDK canonical dump, and Markdown. No other module may redeclare it.
* :data:`EXECUTABLE_STATE_PRECEDENCE` — which state *wins* on a multi-gate
  executable row.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from core.contracts.criteria import AcceptanceCriterion, GateRef

__all__ = [
    "AGENT_STATES",
    "CRITERION_MATRIX_ORDERED_PATHS",
    "CRITERION_STATES",
    "CRITERION_STATE_ORDER",
    "EXECUTABLE_STATES",
    "EXECUTABLE_STATE_PRECEDENCE",
    "HUMAN_STATES",
    "REVIEWER_EXECUTOR",
    "HUMAN_EXECUTOR",
    "CriterionClaim",
    "CriterionMatrix",
    "CriterionRow",
    "HumanDecisionFact",
    "MatrixSummary",
    "ProofRef",
    "build_criterion_matrix",
    "empty_criterion_matrix",
    "gate_state_from_disposition",
]

#: **Canonical serialization order** for every state enumeration (ADR 0188 §3).
#: This is NOT the executable precedence below.
CRITERION_STATE_ORDER: tuple[str, ...] = (
    "proven",
    "failed",
    "stale",
    "missing",
    "not_selected",
    "advisory",
    "accepted",
    "rejected",
    "pending",
)

#: Bundle subtrees whose mapping key order is *data*, not presentation. A
#: serializer that sorts keys must leave these untouched: ``counts_by_state``
#: carries :data:`CRITERION_STATE_ORDER` as its key order, and the whole matrix
#: must stay byte-equivalent with the SDK's canonical JSON. Paths are tuples of
#: keys from the evidence-bundle root.
CRITERION_MATRIX_ORDERED_PATHS: frozenset[tuple[str, ...]] = frozenset({
    ("criterion_matrix",),
})

#: Which state wins on a multi-gate executable row, strongest first.
EXECUTABLE_STATE_PRECEDENCE: tuple[str, ...] = (
    "failed",
    "stale",
    "missing",
    "not_selected",
    "proven",
)

EXECUTABLE_STATES: frozenset[str] = frozenset(EXECUTABLE_STATE_PRECEDENCE)
AGENT_STATES: frozenset[str] = frozenset({"advisory", "pending"})
HUMAN_STATES: frozenset[str] = frozenset({"accepted", "rejected", "pending"})
CRITERION_STATES: frozenset[str] = frozenset(CRITERION_STATE_ORDER)

#: Canonical executors for criteria no task owns.
REVIEWER_EXECUTOR = "reviewer"
HUMAN_EXECUTOR = "human"

_PROOF_KINDS: frozenset[str] = frozenset(
    {"receipt", "finding", "claim", "human_decision"}
)

#: Canonical scheduled-gate disposition -> executable criterion state.
#: The dispositions themselves are produced by
#: :func:`pipeline.verification_ledger.reduce_disposition`; this table only
#: renames them into the criterion vocabulary and never re-derives them.
_DISPOSITION_TO_STATE: dict[str, str] = {
    "executed_pass": "proven",
    "skipped_fresh": "proven",
    "executed_fail": "failed",
    "residual_failed": "failed",
    "residual_stale": "stale",
    "residual_missing": "missing",
    "not_selected": "not_selected",
    "manual_available": "not_selected",
    "suggested": "not_selected",
}


def gate_state_from_disposition(disposition: str | None) -> str:
    """Map a canonical scheduled-gate disposition onto an executable state.

    An identity the ledger does not carry at all is ``missing`` — the honest
    canonical fact, not an invented pass.
    """
    return _DISPOSITION_TO_STATE.get(disposition or "", "missing")


# ── typed reducer inputs ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProofRef:
    """A reference to one durable proof fact. Exactly ``kind`` + ``id``."""

    kind: Literal["receipt", "finding", "claim", "human_decision"]
    id: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class CriterionClaim:
    """A typed developer claim or reviewer finding linked to a criterion.

    ``kind`` selects the proof-ref kind (``claim`` or ``finding``); ``id`` is
    the durable artifact identity. An agent claim can only ever produce
    advisory evidence — this type carries no pass/fail verdict on purpose.
    """

    criterion_id: str
    id: str
    kind: Literal["claim", "finding"] = "claim"
    executor: str = ""


@dataclass(frozen=True, slots=True)
class HumanDecisionFact:
    """The validated head of one criterion's human-decision chain."""

    criterion_id: str
    decision_id: str
    decision: Literal["accept", "reject"]


# ── reducer output ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CriterionRow:
    """One matrix row. Every field is required on the wire."""

    criterion_id: str
    intent: str
    verify: str
    executors: tuple[str, ...]
    method: dict[str, Any]
    proof_refs: tuple[ProofRef, ...]
    state: str
    reason: str
    blocking: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "intent": self.intent,
            "verify": self.verify,
            "executors": list(self.executors),
            "method": _copy_method(self.method),
            "proof_refs": [ref.to_dict() for ref in self.proof_refs],
            "state": self.state,
            "reason": self.reason,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class MatrixSummary:
    """Aggregate readiness view. ``ready`` is exactly ``blocking_open == 0``."""

    total: int
    blocking_open: int
    counts_by_state: dict[str, int]
    pending_human_ids: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.blocking_open == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "blocking_open": self.blocking_open,
            "ready": self.ready,
            "counts_by_state": dict(self.counts_by_state),
            "pending_human_ids": list(self.pending_human_ids),
        }


@dataclass(frozen=True, slots=True)
class CriterionMatrix:
    """Rows in plan order plus the summary final readiness consumes."""

    rows: tuple[CriterionRow, ...] = ()
    summary: MatrixSummary = field(
        default_factory=lambda: MatrixSummary(0, 0, {}, ()),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "summary": self.summary.to_dict(),
        }

    @property
    def ready(self) -> bool:
        return self.summary.ready


def empty_criterion_matrix() -> CriterionMatrix:
    """The explicit new-format empty matrix (distinct from an absent key)."""
    return CriterionMatrix(rows=(), summary=MatrixSummary(0, 0, {}, ()))


def _copy_method(method: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": method["kind"]}
    if "gate_refs" in method:
        out["gate_refs"] = [dict(ref) for ref in method["gate_refs"]]
    if "instructions" in method:
        out["instructions"] = method["instructions"]
    return out


# ── the reduction ────────────────────────────────────────────────────────────


def build_criterion_matrix(
    criteria: Sequence[AcceptanceCriterion],
    *,
    executors_by_criterion: Mapping[str, Sequence[str]] | None = None,
    gate_states: Mapping[tuple[str, str, str], str] | None = None,
    gate_proof_refs: Mapping[tuple[str, str, str], str] | None = None,
    claims: Sequence[CriterionClaim] = (),
    human_decisions: Mapping[str, HumanDecisionFact] | None = None,
) -> CriterionMatrix:
    """Reduce typed facts into exactly one row per criterion, in plan order.

    ``gate_states`` maps a complete ``(command, hook, phase)`` identity to a
    canonical executable state (see :func:`gate_state_from_disposition`); an
    identity absent from the mapping is ``missing``. ``gate_proof_refs`` maps
    the same identity to the durable receipt id that backs it, when one exists.
    """
    owners = {k: tuple(v) for k, v in (executors_by_criterion or {}).items()}
    states = dict(gate_states or {})
    receipts = dict(gate_proof_refs or {})
    decisions = dict(human_decisions or {})
    claims_by_criterion: dict[str, list[CriterionClaim]] = {}
    for claim in claims:
        claims_by_criterion.setdefault(claim.criterion_id, []).append(claim)

    rows: list[CriterionRow] = []
    for criterion in criteria:
        if criterion.verify == "executable":
            rows.append(
                _executable_row(criterion, owners, states, receipts),
            )
        elif criterion.verify == "agent_assertion":
            rows.append(
                _agent_row(
                    criterion, owners, claims_by_criterion.get(criterion.id, []),
                ),
            )
        else:
            rows.append(_human_row(criterion, decisions.get(criterion.id)))

    return CriterionMatrix(rows=tuple(rows), summary=_summarize(tuple(rows)))


def _executors_for(
    criterion: AcceptanceCriterion,
    owners: Mapping[str, tuple[str, ...]],
    fallback: str,
) -> tuple[str, ...]:
    declared = tuple(x for x in owners.get(criterion.id, ()) if x)
    return declared or (fallback,)


def _executable_row(
    criterion: AcceptanceCriterion,
    owners: Mapping[str, tuple[str, ...]],
    states: Mapping[tuple[str, str, str], str],
    receipts: Mapping[tuple[str, str, str], str],
) -> CriterionRow:
    per_ref: list[tuple[GateRef, str, str | None]] = []
    unreceipted: list[GateRef] = []
    for ref in criterion.gate_refs:
        state = states.get(ref.identity, "missing")
        receipt = receipts.get(ref.identity) or None
        # ADR 0188 §3: ``proven`` is a statement about *proof*, so a passing
        # canonical classification with no receipt id behind it cannot carry
        # the row. The honest fact is that the proof artifact is missing —
        # which keeps the row blocking instead of shipping a green row whose
        # ``proof_refs`` is empty.
        if state == "proven" and receipt is None:
            state = "missing"
            unreceipted.append(ref)
        per_ref.append((ref, state, receipt))

    state = _worst_executable_state(tuple(s for _, s, _ in per_ref))
    proof_refs = tuple(
        ProofRef("receipt", receipt)
        for _ref, _s, receipt in per_ref
        if receipt
    )
    losing = [ref.label() for ref, s, _ in per_ref if s == state]
    reason = "" if state == "proven" else f"{state}: " + ", ".join(losing)
    if unreceipted:
        reason += (
            "; a passing gate without a canonical receipt is not proof: "
            + ", ".join(ref.label() for ref in unreceipted)
        )
    return CriterionRow(
        criterion_id=criterion.id,
        intent=criterion.intent,
        verify=criterion.verify,
        executors=_executors_for(criterion, owners, REVIEWER_EXECUTOR),
        method={
            "kind": "gates",
            "gate_refs": [ref.to_dict() for ref in criterion.gate_refs],
        },
        proof_refs=proof_refs,
        state=state,
        reason=reason,
        blocking=state != "proven",
    )


def _worst_executable_state(states: Sequence[str]) -> str:
    if not states:
        return "missing"
    for candidate in EXECUTABLE_STATE_PRECEDENCE:
        if candidate in states:
            return candidate
    return "missing"


def _agent_row(
    criterion: AcceptanceCriterion,
    owners: Mapping[str, tuple[str, ...]],
    claims: Sequence[CriterionClaim],
) -> CriterionRow:
    proof_refs = tuple(ProofRef(claim.kind, claim.id) for claim in claims)
    state = "advisory" if proof_refs else "pending"
    reason = (
        "agent assertion is advisory only; never proven"
        if proof_refs
        else "no typed claim or finding is linked to this criterion"
    )
    return CriterionRow(
        criterion_id=criterion.id,
        intent=criterion.intent,
        verify=criterion.verify,
        executors=_executors_for(criterion, owners, REVIEWER_EXECUTOR),
        method={"kind": "inspection"},
        proof_refs=proof_refs,
        state=state,
        reason=reason,
        blocking=False,
    )


def _human_row(
    criterion: AcceptanceCriterion,
    decision: HumanDecisionFact | None,
) -> CriterionRow:
    if decision is None:
        state, reason, proof_refs = (
            "pending",
            "awaiting a typed operator decision",
            (),
        )
    elif decision.decision == "accept":
        state, reason = "accepted", ""
        proof_refs = (ProofRef("human_decision", decision.decision_id),)
    else:
        state, reason = "rejected", "the operator rejected this criterion"
        proof_refs = (ProofRef("human_decision", decision.decision_id),)
    return CriterionRow(
        criterion_id=criterion.id,
        intent=criterion.intent,
        verify=criterion.verify,
        executors=(HUMAN_EXECUTOR,),
        method={"kind": "manual", "instructions": criterion.human_instructions},
        proof_refs=proof_refs,
        state=state,
        reason=reason,
        blocking=state != "accepted",
    )


def _summarize(rows: Sequence[CriterionRow]) -> MatrixSummary:
    tally: dict[str, int] = {}
    for row in rows:
        tally[row.state] = tally.get(row.state, 0) + 1
    counts = {
        state: tally[state]
        for state in CRITERION_STATE_ORDER
        if tally.get(state, 0) > 0
    }
    return MatrixSummary(
        total=len(rows),
        blocking_open=sum(1 for row in rows if row.blocking),
        counts_by_state=counts,
        pending_human_ids=tuple(
            row.criterion_id
            for row in rows
            if row.verify == "human" and row.state == "pending"
        ),
    )
