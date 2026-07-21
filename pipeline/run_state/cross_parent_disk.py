"""Exact-path durable facts adapter for :mod:`pipeline.run_state.cross_parent`.

The adapter intentionally never discovers children: parent ``projects`` order is
the manifest and every child read is exactly ``run_dir / alias``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.observability.events import read_all
from pipeline.run_state.cross_parent import (
    ActiveOperation,
    CheckpointHandoff,
    ChildFacts,
    CrossParentFacts,
    CrossParentState,
    Observation,
    PendingDecision,
    PhaseIdentity,
    ScheduledGateIdentity,
    reduce_cross_parent_state,
)


def load_cross_parent_facts(run_dir: Path | str) -> CrossParentFacts:
    """Read only the parent and declared child paths required for reduction."""
    path = Path(run_dir)
    parent, parent_observation = _read_object(path / "meta.json")
    checkpoint, checkpoint_observation = _read_object(path / "cross_checkpoint.json")
    aliases = _declared_aliases(parent)
    child_rows = tuple(_child_facts(path, alias, checkpoint) for alias in aliases)
    pending = _decision(parent.get("phase_handoff"), checkpoint)
    return CrossParentFacts(
        declared_aliases=aliases,
        children=child_rows,
        parent_status=_string(parent.get("status")),
        active_operations=_active_operations(path),
        pending_decision=pending,
        checkpoint_handoff=_checkpoint_handoff(checkpoint, checkpoint_observation),
    )


def load_cross_parent_state(run_dir: Path | str) -> CrossParentState:
    """Reduce durable facts from exact paths; this function never writes."""
    return reduce_cross_parent_state(load_cross_parent_facts(run_dir))


def _child_facts(path: Path, alias: str, checkpoint: dict[str, Any]) -> ChildFacts:
    child, observation = _read_object(path / alias / "meta.json")
    return _facts_from_mapping(
        alias,
        child,
        physical=observation,
        checkpoint_sub_status=_string(_mapping(checkpoint.get("sub_status")).get(alias)),
        active_operations=_active_operations(path / alias, alias),
        checkpoint=checkpoint,
    )


def facts_from_session(
    session: dict[str, Any], checkpoint: dict[str, Any], run_dir: Path | str
) -> CrossParentFacts:
    """Combine live session facts with exact physical observations.

    The in-memory child snapshot is deliberately a separate embedded observation;
    it cannot overwrite a conflicting physical child status.
    """
    disk = load_cross_parent_facts(run_dir)
    aliases = _declared_aliases(session) or disk.declared_aliases
    embedded_children = _mapping(_mapping(session.get("phases")).get("projects"))
    by_alias = {child.alias: child for child in disk.children}
    children: list[ChildFacts] = []
    for alias in aliases:
        # A live parent session can name children before its own meta snapshot
        # is flushed.  Read that explicitly declared child path; never infer
        # aliases by scanning the run directory.
        physical = by_alias.get(alias) or _child_facts(Path(run_dir), alias, checkpoint)
        embedded, embedded_observation = _session_object(embedded_children.get(alias))
        children.append(
            ChildFacts(
                alias=alias,
                physical=physical.physical,
                physical_status=physical.physical_status,
                embedded=embedded_observation,
                embedded_status=_string(embedded.get("status")),
                halt_reason=physical.halt_reason or _string(embedded.get("halt_reason")),
                release_verdict=physical.release_verdict or _release(embedded)[0],
                release_ship_ready=(
                    physical.release_ship_ready
                    if physical.release_ship_ready is not None
                    else _release(embedded)[1]
                ),
                pending_decision=physical.pending_decision
                or _decision(embedded.get("phase_handoff"), checkpoint),
                active_operations=physical.active_operations,
                checkpoint_sub_status=physical.checkpoint_sub_status,
            )
        )
    return CrossParentFacts(
        declared_aliases=aliases,
        children=tuple(children),
        parent_status=_string(session.get("status")) or disk.parent_status,
        active_operations=disk.active_operations,
        pending_decision=_decision(session.get("phase_handoff"), checkpoint)
        or disk.pending_decision,
        checkpoint_handoff=_checkpoint_handoff(checkpoint, Observation.PRESENT),
    )


def _facts_from_mapping(
    alias: str,
    value: dict[str, Any],
    *,
    physical: Observation,
    checkpoint_sub_status: str | None,
    active_operations: tuple[ActiveOperation, ...],
    checkpoint: dict[str, Any],
) -> ChildFacts:
    verdict, ship_ready = _release(value)
    return ChildFacts(
        alias=alias,
        physical=physical,
        physical_status=_string(value.get("status")),
        halt_reason=_string(value.get("halt_reason")),
        release_verdict=verdict,
        release_ship_ready=ship_ready,
        pending_decision=_decision(value.get("phase_handoff"), checkpoint),
        active_operations=active_operations,
        checkpoint_sub_status=checkpoint_sub_status,
    )


def _declared_aliases(parent: dict[str, Any]) -> tuple[str, ...]:
    projects = _mapping(parent.get("projects"))
    return tuple(alias for alias in projects if isinstance(alias, str))


def _release(value: dict[str, Any]) -> tuple[str | None, bool | None]:
    release = _mapping(_mapping(value.get("phases")).get("final_acceptance"))
    ship_ready = release.get("ship_ready")
    return _string(release.get("verdict")), ship_ready if isinstance(ship_ready, bool) else None


def _decision(value: Any, checkpoint: dict[str, Any]) -> PendingDecision | None:
    payload = _mapping(value)
    handoff_id = _string(payload.get("id"))
    if handoff_id is None:
        return None
    actions = payload.get("available_actions", payload.get("actions", ()))
    if not isinstance(actions, (list, tuple)) or not all(
        isinstance(action, str) for action in actions
    ):
        actions = ()
    artifacts = _mapping(payload.get("artifacts"))
    alias = _string(payload.get("project_alias")) or _string(artifacts.get("project_alias"))
    child_handoff_id = _string(payload.get("child_handoff_id")) or _string(
        artifacts.get("child_handoff_id")
    )
    # Project proxy payloads carry their exact owner in structured artifacts.
    # This is structural payload data, not an id-prefix inference or a
    # checkpoint-derived routing value.
    kind = _string(payload.get("kind")) or ("project" if alias and child_handoff_id else "")
    return PendingDecision(
        handoff_id=handoff_id,
        kind=kind,
        available_actions=tuple(actions),
        alias=alias,
        parent_handoff_id=handoff_id,
        child_handoff_id=child_handoff_id,
    )


def _checkpoint_handoff(value: dict[str, Any], observation: Observation) -> CheckpointHandoff:
    if observation is Observation.MALFORMED:
        # The repair-facing snapshot historically treats a corrupt optional
        # checkpoint as absent; retain that compatibility in this adapter.
        return CheckpointHandoff()
    return CheckpointHandoff(
        pending=bool(value.get("phase_handoff_pending")),
        kind=_string(value.get("phase_handoff_kind")),
        alias=_string(value.get("phase_handoff_project_alias")),
        parent_handoff_id=_string(value.get("phase_handoff_id")),
        child_handoff_id=_string(value.get("phase_handoff_child_id")),
    )


def _active_operations(path: Path, alias: str | None = None) -> tuple[ActiveOperation, ...]:
    """Fold typed phase/gate lifecycle events, accepting gates declared in ledger."""
    phases: list[ActiveOperation] = []
    gates: list[ActiveOperation] = []
    active_phases: dict[str, ActiveOperation] = {}
    active_gates: dict[tuple[str, str, str], ActiveOperation] = {}
    ledger = _ledger_identities(path)
    for event in read_all(path):
        payload = event.payload
        # Only canonical phase-key lifecycle events represent execution.
        # Presentation banners also emit ``phase.start`` for transcript
        # ordering, but have no ``phase_key`` and often no matching end.
        phase = _string(payload.get("phase_key")) or ""
        if event.kind == "phase.start" and phase:
            active_phases[phase] = ActiveOperation(phase=PhaseIdentity(phase, alias))
        elif event.kind == "phase.end" and phase:
            active_phases.pop(phase, None)
        elif event.kind == "gate.start":
            command = _string(payload.get("command")) or _string(payload.get("name"))
            hook = _string(payload.get("hook"))
            gate_phase = _string(payload.get("phase")) or phase
            identity = (command or "", hook or "", gate_phase or "")
            if command and hook and gate_phase and identity in ledger:
                active_gates[identity] = ActiveOperation(
                    gate=ScheduledGateIdentity(gate_phase, hook, tuple(command.split("\0")), alias)
                )
        elif event.kind == "gate.end":
            command = _string(payload.get("command")) or _string(payload.get("name"))
            hook = _string(payload.get("hook"))
            gate_phase = _string(payload.get("phase")) or phase
            active_gates.pop((command or "", hook or "", gate_phase or ""), None)
    phases.extend(active_phases.values())
    gates.extend(active_gates.values())
    return tuple(phases + gates)


def _ledger_identities(path: Path) -> set[tuple[str, str, str]]:
    ledger, observation = _read_object(path / "scheduled_gate_ledger.json")
    if observation is not Observation.PRESENT:
        return set()
    rows = ledger.get("rows")
    if not isinstance(rows, list):
        return set()
    return {
        (row["command"], row["hook"], row["phase"])
        for row in rows
        if isinstance(row, dict)
        and all(isinstance(row.get(field), str) for field in ("command", "hook", "phase"))
    }


def _read_object(path: Path) -> tuple[dict[str, Any], Observation]:
    if not path.is_file():
        return {}, Observation.MISSING
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, Observation.MALFORMED
    return (value, Observation.PRESENT) if isinstance(value, dict) else ({}, Observation.MALFORMED)


def _session_object(value: Any) -> tuple[dict[str, Any], Observation]:
    if value is None:
        return {}, Observation.MISSING
    return (value, Observation.PRESENT) if isinstance(value, dict) else ({}, Observation.MALFORMED)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["facts_from_session", "load_cross_parent_facts", "load_cross_parent_state"]
