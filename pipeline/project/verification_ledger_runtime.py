# SPDX-License-Identifier: Apache-2.0
"""Run-facing owner of the durable scheduled-gate ledger.

This is intentionally the only adapter that knows both ``PipelineState`` and
the ledger store.  Gate routers record observations here; they never retain an
alternative scheduled trail in ``state.extras``.
"""

from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from pipeline.verification_execution import resolve_execution_eligibility
from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent, build_gate_ledger
from pipeline.verification_ledger_store import (
    ScheduledGateLedger,
    ledger_path,
    load_ledger,
    write_ledger,
)
from pipeline.verification_selection import (
    ScheduledGateEntry,
    ScheduledGatePlan,
    build_scheduled_gate_plan,
    derive_effective_action,
)


class ResumeVerificationLedgerError(RuntimeError):
    """A current plugin cannot execute the durable historical identity safely."""


def initialize(state: Any, *, resume: bool = False) -> ScheduledGateLedger | None:
    """Create the declaration snapshot before any scheduled decision, or load it."""
    contract = getattr(state, "extras", {}).get("verification_contract")
    output_dir = getattr(state, "output_dir", None)
    if contract is None or output_dir is None:
        return None
    run_dir = Path(output_dir)
    path = ledger_path(run_dir)
    if path.exists():
        ledger = load_ledger(run_dir)
        if resume:
            _validate_execution_contract(ledger, contract)
        return ledger
    if resume:
        raise ResumeVerificationLedgerError(
            "resume has a verification contract but no scheduled-gate ledger",
        )
    ledger = ScheduledGateLedger(tuple(
        _materialize_manual_availability(row)
        for row in build_gate_ledger(contract)
    ))
    write_ledger(run_dir, ledger)
    return ledger


def select_epoch(run: Any, contract: Any, *, epoch: str, context: Any) -> ScheduledGatePlan:
    """Resolve once for a fresh epoch, or replay recorded identities on resume."""
    state = run.state
    ledger = initialize(state, resume=bool(getattr(run, "checkpoint_resume", False)))
    if ledger is None:
        # Output-dir-less unit embeddings still need deterministic per-epoch
        # behavior, but the cache is an object-private convenience rather than
        # a durable/extras scheduled trail.
        cache = getattr(state, "_verification_ledger_epoch_cache", None)
        if cache is None:
            cache = {}
            state._verification_ledger_epoch_cache = cache
        if epoch not in cache:
            cache[epoch] = build_scheduled_gate_plan(contract, context)
        return cache[epoch]
    recorded = [event for event in ledger.trail if event.kind == "selection" and event.reason == epoch]
    if recorded:
        return _replay_epoch(ledger, contract, recorded)
    if ledger.finalized:
        raise ResumeVerificationLedgerError("cannot resolve a new epoch for finalized ledger")
    if getattr(run, "checkpoint_resume", False):
        plan = _resolve_snapshot_epoch(ledger, contract, context)
    else:
        plan = build_scheduled_gate_plan(contract, context)
    selected = {(entry.command, entry.hook, entry.phase): entry for entry in plan.entries}
    epoch_hook, epoch_phase = _epoch_identity(epoch)
    trail = list(ledger.trail)
    rows: list[GateLedgerRow] = []
    for row in ledger.rows:
        if row.hook != epoch_hook or row.phase != epoch_phase:
            rows.append(row)
            continue
        entry = selected.get(row.identity)
        is_selected = entry is not None
        trail.append(GateTrailEvent(*row.identity, "selection", "selected" if is_selected else "not_selected", epoch))
        if entry is not None:
            eligibility = resolve_execution_eligibility(
                True, entry.policy, entry.hook, entry.phase,
            )
            rows.append(replace(
                row,
                selected=True,
                execution_policy=entry.policy,
                executor=eligibility.executor,
                trigger=eligibility.trigger,
                consequence=eligibility.consequence,
            ))
        else:
            rows.append(row if row.selected is not None else replace(row, selected=False))
    ledger = ScheduledGateLedger(tuple(rows), tuple(trail), False)
    write_ledger(Path(state.output_dir), ledger)
    return plan


def _materialize_manual_availability(row: GateLedgerRow) -> GateLedgerRow:
    """Persist the operator-owned availability of declared manual-only rows.

    ``manual_only`` is an intentional operator surface, not a lifecycle epoch
    that the engine will select and execute.  It must therefore retain the same
    selected execution facts as an available manual identity when the ledger is
    initialized.  Operator-gated automatic identities remain unresolved until
    their real epoch records an explicit selection decision.
    """
    if row.hook != "manual_only" or row.execution_policy not in {"manual", "suggest"}:
        return row
    eligibility = resolve_execution_eligibility(
        True, row.execution_policy, row.hook, row.phase,
    )
    return replace(
        row,
        selected=True,
        executor=eligibility.executor,
        trigger=eligibility.trigger,
        consequence=eligibility.consequence,
    )


def _epoch_identity(epoch: str) -> tuple[str, str]:
    """Decode the stable ``hook:phase`` lifecycle epoch key."""
    hook, separator, phase = epoch.partition(":")
    if not separator or not hook:
        raise ResumeVerificationLedgerError(f"invalid scheduled-gate epoch {epoch!r}")
    return hook, phase


def record_execution(run: Any, entry: Any, *, passed: bool, receipt_evidence: str | None = None) -> None:
    _append(run, GateTrailEvent(entry.command, entry.hook, entry.phase, "execution", "pass" if passed else "fail", receipt_evidence=receipt_evidence))


def record_reuse(run: Any, entry: Any, *, fresh: bool, receipt_evidence: str | None = None) -> None:
    _append(run, GateTrailEvent(entry.command, entry.hook, entry.phase, "reuse", "fresh" if fresh else "", receipt_evidence=receipt_evidence))


def live_delta(run: Any, since: int = 0) -> tuple[GateTrailEvent, ...]:
    ledger = _load_for_run(run)
    return () if ledger is None else ledger.trail[since:]


def trail_size(run: Any) -> int:
    ledger = _load_for_run(run)
    return 0 if ledger is None else len(ledger.trail)


def finalize(run: Any) -> ScheduledGateLedger | None:
    ledger = _load_for_run(run)
    if ledger is None:
        return None
    closed = ledger.finalize()
    write_ledger(Path(run.state.output_dir), closed)
    return closed


def _append(run: Any, event: GateTrailEvent) -> None:
    ledger = _load_for_run(run)
    if ledger is None:
        state = getattr(run, "state", None)
        ledger = initialize(state) if state is not None else None
    if ledger is None:
        return
    updated = ledger.append(event)
    write_ledger(Path(run.state.output_dir), updated)


def _load_for_run(run: Any) -> ScheduledGateLedger | None:
    state = getattr(run, "state", None)
    output_dir = getattr(state, "output_dir", None)
    if output_dir is None:
        return None
    path = ledger_path(Path(output_dir))
    return load_ledger(Path(output_dir)) if path.exists() else None


def _replay_epoch(ledger: ScheduledGateLedger, contract: Any, events: list[GateTrailEvent]) -> ScheduledGatePlan:
    selected = {event.identity for event in events if event.outcome == "selected"}
    rows = {row.identity: row for row in ledger.rows}
    entries: list[ScheduledGateEntry] = []
    for identity in (row.identity for row in ledger.rows if row.identity in selected):
        row = rows.get(identity)
        if row is None:
            raise ResumeVerificationLedgerError(f"recorded selection has unknown identity {identity!r}")
        _validate_identity_mechanics(row, contract)
        entries.append(ScheduledGateEntry(
            command=row.gate, hook=row.hook, phase=row.phase,
            policy=row.execution_policy, action=_snapshot_action(row, contract),
            contributing_gate_sets=row.gate_sets,
            primary_gate_set=row.gate_sets[0] if row.gate_sets else "",
            activation_binding=row.activation_binding,
        ))
    return _snapshot_plan(entries)


def _resolve_snapshot_epoch(
    ledger: ScheduledGateLedger, contract: Any, context: Any,
) -> ScheduledGatePlan:
    """Resolve a new resume epoch from durable declaration rows, never plugin rules.

    The installed plugin is consulted only after a row has been selected, to
    validate and obtain its executable action.  Its selection declarations are
    deliberately not passed to ``build_scheduled_gate_plan``: a changed plugin
    cannot rewrite historical gate scope mid-run.
    """
    entries: list[ScheduledGateEntry] = []
    for row in ledger.rows:
        if not _snapshot_row_selected(row, context):
            continue
        _validate_identity_mechanics(row, contract)
        entries.append(ScheduledGateEntry(
            command=row.gate, hook=row.hook, phase=row.phase,
            policy=row.execution_policy, action=_snapshot_action(row, contract),
            contributing_gate_sets=row.gate_sets,
            primary_gate_set=row.gate_sets[0] if row.gate_sets else "",
            activation_binding=row.activation_binding,
        ))
    return _snapshot_plan(entries)


def _snapshot_row_selected(row: GateLedgerRow, context: Any) -> bool:
    """Evaluate the normalized snapshot binding against the live epoch inputs."""
    if not row.gate_sets or row.activation_binding == "always":
        return True
    if row.activation_binding == "on_path":
        return any(
            fnmatch(path, pattern)
            for path in getattr(context, "touched_paths", ())
            for pattern in row.condition_paths
        )
    if row.activation_binding == "task_kind":
        return getattr(context, "task_kind", None) in row.selection_task_kinds
    if row.activation_binding == "operator":
        requested = set(getattr(context, "operator_sets", ()))
        return bool(requested.intersection(row.gate_sets))
    return False


def _snapshot_plan(entries: list[ScheduledGateEntry]) -> ScheduledGatePlan:
    gate_sets: list[str] = []
    commands: list[str] = []
    for entry in entries:
        for gate_set in entry.contributing_gate_sets:
            if gate_set not in gate_sets:
                gate_sets.append(gate_set)
        if entry.command not in commands:
            commands.append(entry.command)
    return ScheduledGatePlan(
        entries=tuple(entries), selected_gate_sets=tuple(gate_sets),
        selected_commands=tuple(commands),
    )


def _snapshot_action(row: GateLedgerRow, contract: Any) -> str:
    """Read current execution mechanics only after snapshot identity validation."""
    matching = _matching_schedule_entries(row, contract)
    actions = [entry.action for entry in matching if entry.action is not None]
    if actions:
        from pipeline.verification_contract import GATE_ACTIONS

        return max(actions, key=GATE_ACTIONS.index)
    defaults = [
        contract.gate_sets[name].default_action
        for name in row.gate_sets
        if name in contract.gate_sets
        and contract.gate_sets[name].default_action is not None
    ]
    if defaults:
        from pipeline.verification_contract import GATE_ACTIONS

        return max(defaults, key=GATE_ACTIONS.index)
    return derive_effective_action(row.hook, row.phase, contract.work_mode)


def _validate_execution_contract(ledger: ScheduledGateLedger, contract: Any) -> None:
    for row in ledger.rows:
        _validate_identity_mechanics(row, contract)


def _validate_identity_mechanics(row: GateLedgerRow, contract: Any) -> None:
    if row.gate not in contract.commands:
        raise ResumeVerificationLedgerError(f"resume plugin no longer defines gate {row.gate!r}")
    matching = _matching_schedule_entries(row, contract)
    if not matching:
        raise ResumeVerificationLedgerError(
            f"resume plugin no longer schedules identity {row.identity!r}",
        )
    explicit = {entry.policy for entry in matching if entry.policy is not None}
    if explicit and row.execution_policy not in explicit:
        raise ResumeVerificationLedgerError(
            f"resume execution policy drift for identity {row.identity!r}",
        )
    if row.execution_policy not in {"manual", "suggest", "warn", "require", "unknown"}:
        raise ResumeVerificationLedgerError(f"unsupported historical gate policy {row.execution_policy!r}")


def _matching_schedule_entries(row: GateLedgerRow, contract: Any) -> list[Any]:
    return [
        entry
        for entry in contract.schedule
        if entry.hook == row.hook
        and entry.phase == row.phase
        and (
            row.gate in entry.commands
            or any(
                row.gate in contract.gate_sets[name].commands
                for name in entry.gate_sets
                if name in contract.gate_sets
            )
        )
    ]


__all__ = [
    "ResumeVerificationLedgerError", "finalize", "initialize", "live_delta",
    "record_execution", "record_reuse", "select_epoch", "trail_size",
]
