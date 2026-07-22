"""Immutable structural compilation for a cross-project execution plan.

The graph is deliberately a *plan*, not a ledger.  It records the work that
was admitted and its structural prerequisites, but never readiness, attempts,
or completion.  C1 callers may persist it for inspection without changing the
current dispatch loop.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pipeline.cross_project.task_plan import CrossTaskPlan

__all__ = [
    "CrossExecutionGraph",
    "CrossExecutionGraphCompileError",
    "CrossExecutionGraphCompileIdentity",
    "CrossExecutionGraphNode",
    "CrossExecutionGraphNodeKind",
    "CrossExecutionGraphNodeOwner",
    "CrossExecutionGraphExecutor",
    "CrossExecutionGraphExecutorPolicy",
    "compile_cross_execution_graph",
]


_SCHEMA_VERSION = 1


class CrossExecutionGraphCompileError(ValueError):
    """The admitted plan/profile cannot be represented as a graph."""


class CrossExecutionGraphNodeKind(StrEnum):
    GLOBAL_PHASE = "global_phase"
    PROJECT = "project"
    CONTRACT_CHECK = "contract_check"
    CROSS_FINAL_ACCEPTANCE = "cross_final_acceptance"


class CrossExecutionGraphNodeOwner(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    RUNNER = "runner"


class CrossExecutionGraphExecutor(StrEnum):
    GLOBAL_HANDLER = "global_handler"
    PROJECT_PIPELINE = "project_pipeline"
    RUNNER_GATE = "runner_gate"


@dataclass(frozen=True, slots=True)
class CrossExecutionGraphExecutorPolicy:
    """Structural executor assignment, including a runner gate's policy."""

    executor: CrossExecutionGraphExecutor
    handler: str | None = None
    enabled: bool = True
    run: str | None = None
    on_skip: str | None = None
    mode: str | None = None

    @property
    def runnable(self) -> bool:
        """Whether this structural node is eligible for execution.

        This derived convenience is intentionally not persisted as ledger
        state.  A disabled or ``run=never`` gate remains a graph node.
        """
        return self.enabled and self.run != "never"


@dataclass(frozen=True, slots=True)
class CrossExecutionGraphNode:
    """One opaque structural node in a compiled graph."""

    identity: str
    kind: CrossExecutionGraphNodeKind
    dependencies: tuple[str, ...]
    owner: CrossExecutionGraphNodeOwner
    executor: CrossExecutionGraphExecutorPolicy
    required: bool = True


@dataclass(frozen=True, slots=True)
class CrossExecutionGraphCompileIdentity:
    schema_version: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CrossExecutionGraph:
    compile_identity: CrossExecutionGraphCompileIdentity
    nodes: tuple[CrossExecutionGraphNode, ...]


def _value(value: Any) -> str | None:
    """Return enum/string policy values without coupling to runtime classes."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _opaque_identity(category: str, value: str) -> str:
    digest = hashlib.sha256(f"cross-graph-v{_SCHEMA_VERSION}\0{category}\0{value}".encode()).hexdigest()
    return f"n{digest[:24]}"


def _declared_aliases(owners: Sequence[str] | Mapping[str, Any]) -> tuple[str, ...]:
    values = tuple(owners.keys()) if isinstance(owners, Mapping) else tuple(owners)
    if any(not isinstance(alias, str) or not alias.strip() for alias in values):
        raise CrossExecutionGraphCompileError("declared owner identity must be a non-empty string")
    if len(set(values)) != len(values):
        raise CrossExecutionGraphCompileError("duplicate declared owner identity")
    return values


def _global_entries(profile_setup: Any) -> tuple[Any, ...]:
    projection = getattr(profile_setup, "projection", None)
    entries = getattr(projection, "global_steps", None)
    if entries is None:
        raise CrossExecutionGraphCompileError("profile setup has no global_steps projection")
    flattened: list[Any] = []
    for entry in entries:
        # LoopStep is structurally expanded only once: retry rounds are runtime
        # behaviour, never graph nodes.
        steps = getattr(entry, "steps", None)
        if isinstance(steps, tuple):
            flattened.extend(steps)
        else:
            flattened.append(entry)
    return tuple(flattened)


def _validate_project_entries(profile_setup: Any) -> bool:
    """Validate that projected child entries have one executor assignment."""
    projection = getattr(profile_setup, "projection", None)
    entries = getattr(projection, "project_steps", None)
    if entries is None:
        raise CrossExecutionGraphCompileError("profile setup has no project_steps projection")
    for entry in entries:
        inner = getattr(entry, "steps", None)
        candidates = inner if isinstance(inner, tuple) else (entry,)
        for step in candidates:
            phase = getattr(step, "phase", None)
            if not isinstance(phase, str) or not phase.strip():
                raise CrossExecutionGraphCompileError("unassignable project profile entry")
    return bool(entries)


def _gate_executor(policy: Any) -> CrossExecutionGraphExecutorPolicy:
    if policy is None:
        raise CrossExecutionGraphCompileError("runner-owned gate has no effective policy")
    enabled = bool(getattr(policy, "enabled", False))
    run = _value(getattr(policy, "run", None))
    on_skip = _value(getattr(policy, "on_skip", None))
    mode = _value(getattr(policy, "mode", None))
    if run not in {"always", "auto", "manual_confirm", "never"}:
        raise CrossExecutionGraphCompileError(f"unassignable gate run policy {run!r}")
    if on_skip not in {"block", "allow_with_gap", "allow"}:
        raise CrossExecutionGraphCompileError(f"unassignable gate on_skip policy {on_skip!r}")
    return CrossExecutionGraphExecutorPolicy(
        executor=CrossExecutionGraphExecutor.RUNNER_GATE,
        enabled=enabled,
        run=run,
        on_skip=on_skip,
        mode=mode,
    )


def _fingerprint(nodes: tuple[CrossExecutionGraphNode, ...]) -> str:
    structural = {
        "schema_version": _SCHEMA_VERSION,
        "nodes": [
            {
                "identity": node.identity,
                "kind": node.kind.value,
                "dependencies": node.dependencies,
                "owner": node.owner.value,
                "required": node.required,
                "executor": {
                    "executor": node.executor.executor.value,
                    "handler": node.executor.handler,
                    "enabled": node.executor.enabled,
                    "run": node.executor.run,
                    "on_skip": node.executor.on_skip,
                    "mode": node.executor.mode,
                },
            }
            for node in nodes
        ],
    }
    payload = json.dumps(structural, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compile_cross_execution_graph(
    plan: CrossTaskPlan,
    declared_owners: Sequence[str] | Mapping[str, Any],
    profile_setup: Any,
) -> CrossExecutionGraph:
    """Purely compile admitted cross inputs into a stable, immutable graph.

    ``declared_owners`` supplies the cross invocation's alias order.  It is
    deliberately separate from plan prose; only ``CrossTaskUnit.depends_on``
    creates project-to-project edges.
    """
    if not isinstance(plan, CrossTaskPlan):
        raise CrossExecutionGraphCompileError("plan must be a CrossTaskPlan")
    owners = _declared_aliases(declared_owners)
    owner_set = set(owners)

    aliases: set[str] = set()
    unit_ids: set[str] = set()
    units: dict[str, Any] = {}
    for unit in plan.units:
        alias, unit_id = getattr(unit, "alias", None), getattr(unit, "unit_id", None)
        if not isinstance(alias, str) or not alias.strip() or not isinstance(unit_id, str) or not unit_id.strip():
            raise CrossExecutionGraphCompileError("project unit identity must be a non-empty string")
        if alias in aliases:
            raise CrossExecutionGraphCompileError(f"duplicate project alias {alias!r}")
        if unit_id in unit_ids:
            raise CrossExecutionGraphCompileError(f"duplicate project unit identity {unit_id!r}")
        if alias not in owner_set:
            raise CrossExecutionGraphCompileError(f"unknown owner {alias!r} for project unit")
        aliases.add(alias)
        unit_ids.add(unit_id)
        units[alias] = unit
    missing = owner_set - aliases
    if missing:
        raise CrossExecutionGraphCompileError(f"declared owner without project unit {sorted(missing)[0]!r}")

    # Validate every dependency before ordering, including manual plans that
    # bypassed the JSON parser.
    project_dependencies: dict[str, tuple[str, ...]] = {}
    for alias in owners:
        # The current schema admission accepts repeated dependency entries.
        # Treat them as one structural edge so compiling an admitted plan does
        # not change C1's existing live-dispatch behaviour.  ``dict`` keeps
        # declaration order, which makes both topology and fingerprint stable.
        deps = tuple(dict.fromkeys(getattr(units[alias], "depends_on", ())))
        for dependency in deps:
            if dependency not in units:
                raise CrossExecutionGraphCompileError(
                    f"dangling dependency {dependency!r} for project {alias!r}"
                )
            if dependency == alias:
                raise CrossExecutionGraphCompileError(f"self dependency for project {alias!r}")
        project_dependencies[alias] = deps

    global_nodes: list[CrossExecutionGraphNode] = []
    previous: str | None = None
    global_phases: set[str] = set()
    for index, step in enumerate(_global_entries(profile_setup)):
        phase = getattr(step, "phase", None)
        handler = getattr(getattr(step, "cross", None), "handler", None)
        if not isinstance(phase, str) or not phase.strip() or not isinstance(handler, str) or not handler.strip():
            raise CrossExecutionGraphCompileError("unassignable global profile entry")
        if phase in global_phases:
            raise CrossExecutionGraphCompileError(f"duplicate global phase identity {phase!r}")
        global_phases.add(phase)
        identity = _opaque_identity("global", f"{index}:{phase}:{handler}")
        global_nodes.append(CrossExecutionGraphNode(
            identity=identity,
            kind=CrossExecutionGraphNodeKind.GLOBAL_PHASE,
            dependencies=(previous,) if previous else (),
            owner=CrossExecutionGraphNodeOwner.GLOBAL,
            executor=CrossExecutionGraphExecutorPolicy(
                executor=CrossExecutionGraphExecutor.GLOBAL_HANDLER, handler=handler,
            ),
        ))
        previous = identity

    # Stable Kahn sorting gives dependency order without changing the declared
    # input order of independent aliases.
    position = {alias: index for index, alias in enumerate(owners)}
    indegree = {alias: len(project_dependencies[alias]) for alias in owners}
    dependents = {alias: [] for alias in owners}
    for alias, dependencies in project_dependencies.items():
        for dependency in dependencies:
            dependents[dependency].append(alias)
    available = [alias for alias in owners if indegree[alias] == 0]
    ordered_aliases: list[str] = []
    while available:
        available.sort(key=position.__getitem__)
        alias = available.pop(0)
        ordered_aliases.append(alias)
        for dependent in dependents[alias]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                available.append(dependent)
    if len(ordered_aliases) != len(owners):
        raise CrossExecutionGraphCompileError("cycle in project dependencies")

    projects_required = _validate_project_entries(profile_setup)
    project_ids = {alias: _opaque_identity("project", units[alias].unit_id) for alias in owners}
    project_nodes = [
        CrossExecutionGraphNode(
            identity=project_ids[alias],
            kind=CrossExecutionGraphNodeKind.PROJECT,
            dependencies=tuple(
                ([previous] if previous else []) + [project_ids[dep] for dep in project_dependencies[alias]]
            ),
            owner=CrossExecutionGraphNodeOwner.PROJECT,
            executor=CrossExecutionGraphExecutorPolicy(
                executor=CrossExecutionGraphExecutor.PROJECT_PIPELINE,
            ),
            required=projects_required,
        )
        for alias in ordered_aliases
    ]
    required_projects = tuple(node.identity for node in project_nodes if node.required)
    contract_dependencies = required_projects or ((previous,) if previous else ())
    contract_id = _opaque_identity("runner", "contract_check")
    contract = CrossExecutionGraphNode(
        identity=contract_id,
        kind=CrossExecutionGraphNodeKind.CONTRACT_CHECK,
        dependencies=contract_dependencies,
        owner=CrossExecutionGraphNodeOwner.RUNNER,
        executor=_gate_executor(getattr(profile_setup, "contract_gate_policy", None)),
    )
    cfa = CrossExecutionGraphNode(
        identity=_opaque_identity("runner", "cross_final_acceptance"),
        kind=CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE,
        dependencies=(contract_id,),
        owner=CrossExecutionGraphNodeOwner.RUNNER,
        executor=_gate_executor(getattr(profile_setup, "cfa_gate_policy", None)),
    )
    nodes = tuple(global_nodes + project_nodes + [contract, cfa])
    identities = [node.identity for node in nodes]
    if len(set(identities)) != len(identities):  # defensive future-proofing
        raise CrossExecutionGraphCompileError("duplicate compiled node identity")
    return CrossExecutionGraph(
        compile_identity=CrossExecutionGraphCompileIdentity(_SCHEMA_VERSION, _fingerprint(nodes)),
        nodes=nodes,
    )
