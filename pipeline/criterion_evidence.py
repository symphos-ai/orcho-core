# SPDX-License-Identifier: Apache-2.0
"""pipeline.criterion_evidence — durable facts in, criterion matrix out.

:mod:`pipeline.criterion_matrix` is pure. This thin adapter is the only place
that reads a run directory to feed it, so every projection (evidence JSON,
Markdown, SDK, CLI, MCP) composes the *same* durable facts after a resume as
during the live run:

* the accepted plan artifact — typed criteria and per-task ``acceptance_refs``;
* the scheduled-gate ledger — canonical per-identity dispositions and the
  receipt evidence backing them;
* the durable typed claim log plus criterion-linked reviewer findings;
* the durable human-decision chains.

It re-derives nothing. Gate freshness and selection stay owned by the
verification authorities that wrote the ledger.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.contracts.criteria import AcceptanceCriterion
from pipeline.criterion_claims import reducer_claims
from pipeline.criterion_decisions import human_decision_facts
from pipeline.criterion_matrix import (
    CriterionMatrix,
    build_criterion_matrix,
    gate_state_from_disposition,
)

__all__ = [
    "collect_criterion_matrix",
    "criterion_matrix_for_run",
    "executors_from_plan",
    "gate_facts_from_ledger",
]


def executors_from_plan(subtasks: Sequence[Any]) -> dict[str, tuple[str, ...]]:
    """Map criterion id -> owning task ids, in plan order.

    Coverage is a *validated* plan property (see
    :func:`core.contracts.plan_schema.validate_plan_dict`); this function only
    projects it. A criterion no task references simply gets no entry, and the
    reducer falls back to its class's canonical executor.
    """
    owners: dict[str, list[str]] = {}
    for subtask in subtasks:
        for ref in getattr(subtask, "acceptance_refs", ()) or ():
            owners.setdefault(str(ref), []).append(str(subtask.id))
    return {k: tuple(v) for k, v in owners.items()}


def gate_facts_from_ledger(
    run_dir: Path | str,
) -> tuple[dict[tuple[str, str, str], str], dict[tuple[str, str, str], str]]:
    """Return ``(state_by_identity, receipt_by_identity)`` from the durable ledger.

    An **absent** ledger yields empty mappings: the project declares no
    verification contract, so the reducer honestly reports every executable ref
    as ``missing``. A **present but unreadable** ledger raises
    :class:`~pipeline.verification_ledger_store.LedgerStoreError` — a corrupt
    proof authority must not be projected as "no proof yet", which would look
    identical to a run that simply has not executed its gates.
    """
    from pipeline.verification_ledger_store import ledger_path, load_ledger

    if not ledger_path(Path(run_dir)).exists():
        return {}, {}
    ledger = load_ledger(Path(run_dir))

    states: dict[tuple[str, str, str], str] = {}
    receipts: dict[tuple[str, str, str], str] = {}
    for row in ledger.rows:
        states[row.identity] = gate_state_from_disposition(row.disposition)
        if row.receipt_evidence:
            receipts[row.identity] = str(row.receipt_evidence)
    return states, receipts


def collect_criterion_matrix(
    run_dir: Path | str,
    criteria: Sequence[AcceptanceCriterion],
    *,
    subtasks: Sequence[Any] = (),
    findings: Iterable[Mapping[str, Any]] = (),
) -> CriterionMatrix:
    """Build the criterion matrix for ``run_dir`` from durable facts only."""
    states, receipts = gate_facts_from_ledger(run_dir)
    return build_criterion_matrix(
        criteria,
        executors_by_criterion=executors_from_plan(subtasks),
        gate_states=states,
        gate_proof_refs=receipts,
        claims=reducer_claims(run_dir, findings=findings),
        human_decisions=human_decision_facts(run_dir),
    )


def criterion_matrix_for_run(
    run_dir: Path | str,
    *,
    findings: Iterable[Mapping[str, Any]] = (),
) -> CriterionMatrix | None:
    """The run's criterion matrix from its accepted-plan artifact, or ``None``.

    ``None`` means the artifact is genuinely **absent** — a legacy run, or one
    that never produced a plan — so there is no criterion contract to report. A
    plan with zero criteria returns the explicit empty matrix instead.

    A plan artifact that exists but does not load, a corrupt claim log, and a
    malformed decision journal all **raise**. Silently degrading any of those
    to "no matrix" would erase a blocking criterion from every readiness
    consumer, which is precisely the fail-open this contract exists to prevent.

    Reading the artifact (rather than an in-memory plan) is what makes a
    resumed run rebuild the same matrix from the same durable facts.
    """
    from pipeline.plan_artifacts import (
        LATEST_FILENAME,
        PARSED_PLAN_ARTIFACT_VERSION,
        load_parsed_plan_artifact,
    )

    artifact_path = Path(run_dir) / LATEST_FILENAME
    if not artifact_path.is_file():
        return None
    # A version-1 plan artifact may predate ADR 0188. The explicit absence of
    # ``acceptance_criteria`` identifies that legacy shape and therefore an
    # absent criterion contract. Once the field exists, even as ``[]``, the
    # artifact is new-format and every authority is loaded strictly below.
    # Unknown versions and malformed envelopes still reach the strict loader.
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Let the canonical strict loader normalize the failure into its
        # established parsed-plan error contract below.
        raw = None
    body = raw.get("plan") if isinstance(raw, Mapping) else None
    if (
        isinstance(raw, Mapping)
        and raw.get("artifact_version") == PARSED_PLAN_ARTIFACT_VERSION
        and isinstance(body, Mapping)
        and "acceptance_criteria" not in body
    ):
        return None
    plan = load_parsed_plan_artifact(Path(run_dir))
    return collect_criterion_matrix(
        run_dir, plan.acceptance_criteria, subtasks=plan.subtasks, findings=findings,
    )
