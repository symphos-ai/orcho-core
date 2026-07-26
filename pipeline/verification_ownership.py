"""Deterministic verification-ownership checks for parsed plans.

The verification contract owns official scheduled commands.  A parsed plan may
still contain targeted, supplemental checks, but it must not materialize an
exact selected engine-owned command as an implementation command.  This module
owns that post-parse comparison; it does not execute commands, infer shell
semantics, or mutate the plan.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
    resolve_placeholders,
)
from pipeline.verification_execution import (
    VerificationIdentity,
    resolve_selected_execution,
)
from pipeline.verification_selection import (
    build_scheduled_gate_plan,
    selection_context_from_extras,
)


@dataclass(frozen=True)
class VerificationOwnershipConflict:
    """One exact plan-command overlap with an engine-owned scheduled gate."""

    plan_index: int
    plan_command: str
    gate_command: str
    hook: str
    phase: str

    @property
    def location(self) -> str:
        return f"commands_to_run[{self.plan_index}]"


def find_verification_ownership_conflicts(
    plan: Any,
    contract: VerificationContract | None,
    extras: Mapping[str, Any],
) -> tuple[VerificationOwnershipConflict, ...]:
    """Return exact overlaps between plan commands and effective engine gates.

    Matching is intentionally limited to normalized argv equality.  It does not
    infer equivalence from prose, wrappers, aliases, environment expansion, or
    shared executables such as ``pytest``.  Planned paths drive path selection
    so the comparison uses the gate identities expected for this plan rather
    than the still-clean pre-implement checkout.
    """
    if contract is None:
        return ()

    scheduled = build_planned_verification_gate_plan(plan, contract, extras)
    placeholders = extras.get("verification_placeholders")
    if not isinstance(placeholders, PlaceholderContext):
        placeholders = PlaceholderContext()

    engine_commands: dict[tuple[str, ...], tuple[str, str, str]] = {}
    for entry in scheduled.entries:
        execution = resolve_selected_execution(VerificationIdentity(
            command=entry.command,
            hook=entry.hook,
            phase=entry.phase,
            policy=entry.policy,
        ))
        if execution.executor != "engine":
            continue
        argv = _declared_argv(contract, entry.command, placeholders)
        if argv:
            engine_commands.setdefault(
                argv,
                (entry.command, entry.hook, entry.phase),
            )

    conflicts: list[VerificationOwnershipConflict] = []
    for index, command in enumerate(
        tuple(getattr(plan, "commands_to_run", ()) or ()),
    ):
        argv = _plan_argv(command)
        matched = engine_commands.get(argv)
        if matched is None:
            continue
        gate_command, hook, phase = matched
        conflicts.append(VerificationOwnershipConflict(
            plan_index=index,
            plan_command=str(command),
            gate_command=gate_command,
            hook=hook,
            phase=phase,
        ))
    return tuple(conflicts)


def build_planned_verification_gate_plan(
    plan: Any,
    contract: VerificationContract,
    extras: Mapping[str, Any],
) -> Any:
    """Resolve scheduled gates against the parsed plan's anticipated paths."""
    return build_scheduled_gate_plan(
        contract,
        selection_context_from_extras(
            extras,
            contract,
            touched_paths=_planned_paths(plan),
        ),
    )


def render_verification_ownership_rejection(
    conflicts: tuple[VerificationOwnershipConflict, ...],
) -> str:
    """Render a valid validate-plan review for deterministic conflicts."""
    details = "; ".join(
        f"{conflict.location} duplicates {conflict.gate_command!r} "
        f"({conflict.hook}:{conflict.phase or '-'})"
        for conflict in conflicts
    )
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": (
            "The plan duplicates selected engine-owned verification commands."
        ),
        "findings": [{
            "id": "verification-ownership",
            "severity": "P1",
            "title": "Engine-owned verification moved into implementation",
            "body": (
                "Exact post-parse command overlap was detected: "
                f"{details}. Scheduled gates are executed and recorded by the "
                "engine, not by implement subtasks."
            ),
            "required_fix": (
                "Remove the overlapping commands from commands_to_run and keep "
                "selected engine gates out of task specs and done criteria. "
                "Retain only targeted checks for the concrete change; express "
                "repository-wide verification as engine gate policy."
            ),
        }],
        "risks": [],
        "checks": [
            "Compared parsed commands_to_run with effective scheduled gate "
            "identities by exact normalized argv."
        ],
    })


def _planned_paths(plan: Any) -> tuple[str, ...]:
    ordered: list[str] = []

    def add(values: Any) -> None:
        for value in tuple(values or ()):
            text = str(value)
            if text and text not in ordered:
                ordered.append(text)

    add(getattr(plan, "owned_files", ()))
    add(getattr(plan, "file_paths", ()))
    for task in tuple(getattr(plan, "subtasks", ()) or ()):
        add(getattr(task, "owned_files", ()))
        add(getattr(task, "files", ()))
    return tuple(ordered)


def _declared_argv(
    contract: VerificationContract,
    command: str,
    placeholders: PlaceholderContext,
) -> tuple[str, ...]:
    declaration = contract.commands.get(command, {})
    run = declaration.get("run", "")
    if isinstance(run, (list, tuple)):
        return tuple(
            resolve_placeholders(str(token), placeholders)
            for token in run
        )
    rendered = resolve_placeholders(str(run), placeholders)
    return _split(rendered)


def _plan_argv(command: Any) -> tuple[str, ...]:
    return _split(str(command))


def _split(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return ()


__all__ = [
    "VerificationOwnershipConflict",
    "build_planned_verification_gate_plan",
    "find_verification_ownership_conflicts",
    "render_verification_ownership_rejection",
]
