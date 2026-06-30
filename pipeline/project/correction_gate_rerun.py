# SPDX-License-Identifier: Apache-2.0
"""Execute the ``gate_rerun`` correction shortcut.

``correction_triage`` can classify a follow-up as ``gate_rerun``: no code
change is needed, but the current child run must materialise fresh verification
receipts before ``final_acceptance`` reads readiness. Skipping straight to
``final_acceptance`` leaves the child run without receipts and creates an
operator loop. This module is the narrow executor for that route.

It owns no runner of its own anymore: it builds the run context and delegates to
the shared :func:`pipeline.project.verification_autorun.materialize_required_receipts`,
then adapts the typed :class:`~pipeline.project.verification_autorun.ReceiptAutoRunResult`
back into the historical ``gate_rerun_execution`` evidence shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ``_workspace_for_run_dir`` now lives with the shared executor; re-export it
# here so existing importers (and the run-context builder below) keep working.
# ``ReceiptAutoRunResult`` / ``_record_autorun_evidence`` are reused so the
# gate-rerun pass also writes the durable Stage 9 auto-run trail (so the DONE
# aggregator sees its fresh/skipped_manual commands) without forking the shape.
from pipeline.project.verification_autorun import (  # noqa: F401 — re-export
    ReceiptAutoRunResult,
    _record_autorun_evidence,
    _workspace_for_run_dir,
)
from pipeline.verification_receipt_index import VERIFICATION_PARENT_RUNS_EXTRAS_KEY

_GATE_RERUN_REASON = "required verification receipts rerun for current correction run"

# The gate-rerun pass is triggered at the ``correction_triage`` phase boundary;
# its durable auto-run trail entry is recorded under that phase.
_GATE_RERUN_PHASE = "correction_triage"


@dataclass(frozen=True)
class GateRerunExecution:
    """Durable evidence for a correction gate-rerun attempt.

    A thin adapter over :class:`~pipeline.project.verification_autorun.ReceiptAutoRunResult`
    that preserves the historical ``gate_rerun_execution`` evidence keys
    (``attempted`` / ``reason`` / ``env`` / ``env_passed`` / ``required_passed``
    / ``receipts`` / ``error``).
    """

    attempted: bool
    reason: str
    env: str = ""
    env_passed: bool | None = None
    required_passed: bool | None = None
    receipts: tuple[str, ...] = ()
    error: str = ""

    def to_evidence(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "reason": self.reason,
            "env": self.env,
            "env_passed": self.env_passed,
            "required_passed": self.required_passed,
            "receipts": list(self.receipts),
            "error": self.error,
        }


def _execution_from_result(
    result: ReceiptAutoRunResult, *, env: str,
) -> GateRerunExecution:
    """Adapt a shared-executor result into the gate-rerun evidence shape.

    Both flags are derived from the *authoritative on-disk state* the
    materializer observed after its pass, not from this call's optimistic exit
    codes — the F2 "never falsely green" invariant.

    ``env_passed`` is ``True`` only when no verify_env raised AND no env receipt
    came back un-passed (``result.failed_envs`` empty): a verify_env that returns
    ``all_passed=False`` and writes a failed env receipt must not read as green.

    ``required_passed`` is ``True`` only when the materialization left no failed
    command receipt, no executor error, no withheld manual/operator-only required
    command, AND no required command still classified ``missing``/``stale`` once
    the receipts were re-read from disk (``result.residual_required`` empty) — so
    an exit-0 command whose receipt never landed, or one left stale by dependency
    drift, can never green the gate. It stays ``None`` for the no-op (not
    attempted) path where no classification ran.
    """
    if not result.attempted:
        return GateRerunExecution(attempted=False, reason=result.reason)

    env_errors = [e for e in result.errors if e.startswith("verify_env")]
    env_passed = not env_errors and not result.failed_envs
    required_passed = (
        not result.failed
        and not result.errors
        and not result.skipped_manual
        and not result.residual_required
    )
    return GateRerunExecution(
        attempted=True,
        reason=result.reason,
        env=env,
        env_passed=env_passed,
        required_passed=required_passed,
        receipts=result.receipt_paths,
        error="; ".join(result.errors),
    )


def execute_gate_rerun_receipts(
    run: Any,
) -> tuple[GateRerunExecution, ReceiptAutoRunResult]:
    """Run current-child required verification receipts for ``gate_rerun``.

    Builds the run context (output dir, project, projected contract +
    placeholder ctx, parent receipt sources) and delegates to the shared
    materializer. No-op unless the run has an output dir. Contract / empty
    ``required`` no-ops and every :mod:`sdk.verify` failure are handled by the
    materializer and surfaced as evidence — never raised: ``final_acceptance``
    is the authoritative release gate and will reject failed/missing receipts.

    Returns the historical :class:`GateRerunExecution` (whose ADR-fixed
    ``gate_rerun_execution`` evidence keys are unchanged) *paired with* the raw
    :class:`~pipeline.project.verification_autorun.ReceiptAutoRunResult` so the
    caller can render the live block (which needs the rerun command *names* that
    ``GateRerunExecution.to_evidence()`` deliberately does not carry). The raw
    result is also appended to the durable
    ``state.extras['verification_autorun']`` trail via
    :func:`_record_autorun_evidence` — the same fixed shape the Stage 9 auto-run
    records — so the DONE aggregator counts this gate-rerun's fresh /
    skipped_manual commands even on a gate-rerun-only run (where the final-phase
    auto-run is skipped).
    """
    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        result = ReceiptAutoRunResult(
            attempted=False,
            reason="no output_dir; cannot write current-run verification receipts",
        )
        _record_autorun_evidence(run, _GATE_RERUN_PHASE, result)
        return _execution_from_result(result, env=""), result

    extras = getattr(getattr(run, "state", None), "extras", {}) or {}
    contract = extras.get("verification_contract")
    ctx = extras.get("verification_placeholders")
    parent_sources = extras.get(VERIFICATION_PARENT_RUNS_EXTRAS_KEY, ())

    run_dir = Path(output_dir)
    project = str(run.project_path)
    checkout = getattr(ctx, "checkout", "") or project
    workspace = _workspace_for_run_dir(run_dir)

    from pipeline.project.verification_autorun import materialize_required_receipts

    result = materialize_required_receipts(
        run_id=run_dir.name,
        run_dir=run_dir,
        project_dir=project,
        checkout=checkout,
        contract=contract,
        ctx=ctx,
        workspace=workspace,
        parent_sources=parent_sources,
        # Full state.extras so the materializer reuses the cached before_delivery
        # routing plan (path-selected delivery gates) the same way readiness and
        # the delivery gate do. parent_sources already lives inside ``extras``
        # under VERIFICATION_PARENT_RUNS_EXTRAS_KEY, so passing both is not
        # double-counted (the materializer's fallback merge is skipped when the
        # key is already present).
        extras=extras,
        dry_run=False,
        reason=_GATE_RERUN_REASON,
    )
    # Durable trail (same fixed shape as the Stage 9 auto-run) so DONE sees the
    # gate-rerun's fresh/skipped_manual commands.
    _record_autorun_evidence(run, _GATE_RERUN_PHASE, result)
    execution = _execution_from_result(
        result, env=getattr(contract, "default_env", "") or "",
    )
    return execution, result
