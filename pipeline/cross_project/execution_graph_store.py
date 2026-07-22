"""Strict, crash-safe persistence for the immutable cross execution graph."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphCompileIdentity,
    CrossExecutionGraphExecutor,
    CrossExecutionGraphExecutorPolicy,
    CrossExecutionGraphNode,
    CrossExecutionGraphNodeKind,
    CrossExecutionGraphNodeOwner,
    _fingerprint,
)

__all__ = [
    "CROSS_EXECUTION_GRAPH_FILENAME",
    "CROSS_EXECUTION_GRAPH_SCHEMA_VERSION",
    "CrossExecutionGraphStoreError",
    "load_cross_execution_graph",
    "serialize_cross_execution_graph",
    "write_cross_execution_graph",
]


CROSS_EXECUTION_GRAPH_FILENAME = "cross_execution_graph.json"
CROSS_EXECUTION_GRAPH_SCHEMA_VERSION = 1


class CrossExecutionGraphStoreError(ValueError):
    """The graph snapshot is absent, malformed, or conflicts with a run."""


def _expect_object(value: Any, context: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CrossExecutionGraphStoreError(f"{context} must be an object")
    actual = set(value)
    if actual != keys:
        extra = actual - keys
        missing = keys - actual
        detail = f"unexpected fields {sorted(extra)}" if extra else f"missing fields {sorted(missing)}"
        raise CrossExecutionGraphStoreError(f"{context} has {detail}")
    return value


def _expect_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CrossExecutionGraphStoreError(f"{context} must be a non-empty string")
    return value


def _enum(enum_type: type, value: Any, context: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise CrossExecutionGraphStoreError(f"invalid {context}: {value!r}") from exc


def serialize_cross_execution_graph(graph: CrossExecutionGraph) -> bytes:
    """Encode the closed v1 structural wire format as deterministic JSON."""
    _validate_graph(graph)
    wire = {
        "schema_version": CROSS_EXECUTION_GRAPH_SCHEMA_VERSION,
        "compile_identity": {
            "schema_version": graph.compile_identity.schema_version,
            "fingerprint": graph.compile_identity.fingerprint,
        },
        "nodes": [
            {
                "identity": node.identity,
                "kind": node.kind.value,
                "dependencies": list(node.dependencies),
                "owner": node.owner.value,
                "executor": {
                    "executor": node.executor.executor.value,
                    "handler": node.executor.handler,
                    "enabled": node.executor.enabled,
                    "run": node.executor.run,
                    "on_skip": node.executor.on_skip,
                    "mode": node.executor.mode,
                },
                "required": node.required,
            }
            for node in graph.nodes
        ],
    }
    return (json.dumps(wire, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _parse_node(value: Any, index: int) -> CrossExecutionGraphNode:
    node = _expect_object(
        value, f"nodes[{index}]",
        {"identity", "kind", "dependencies", "owner", "executor", "required"},
    )
    dependencies = node["dependencies"]
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
        raise CrossExecutionGraphStoreError(f"nodes[{index}].dependencies must be a list of identities")
    if len(set(dependencies)) != len(dependencies):
        raise CrossExecutionGraphStoreError(f"nodes[{index}].dependencies contains duplicates")
    if not isinstance(node["required"], bool):
        raise CrossExecutionGraphStoreError(f"nodes[{index}].required must be a bool")
    executor = _expect_object(
        node["executor"], f"nodes[{index}].executor",
        {"executor", "handler", "enabled", "run", "on_skip", "mode"},
    )
    if executor["handler"] is not None and not isinstance(executor["handler"], str):
        raise CrossExecutionGraphStoreError(f"nodes[{index}].executor.handler must be string or null")
    for field in ("run", "on_skip", "mode"):
        if executor[field] is not None and not isinstance(executor[field], str):
            raise CrossExecutionGraphStoreError(f"nodes[{index}].executor.{field} must be string or null")
    if not isinstance(executor["enabled"], bool):
        raise CrossExecutionGraphStoreError(f"nodes[{index}].executor.enabled must be a bool")
    return CrossExecutionGraphNode(
        identity=_expect_string(node["identity"], f"nodes[{index}].identity"),
        kind=_enum(CrossExecutionGraphNodeKind, node["kind"], f"nodes[{index}].kind"),
        dependencies=tuple(dependencies),
        owner=_enum(CrossExecutionGraphNodeOwner, node["owner"], f"nodes[{index}].owner"),
        executor=CrossExecutionGraphExecutorPolicy(
            executor=_enum(CrossExecutionGraphExecutor, executor["executor"], f"nodes[{index}].executor.executor"),
            handler=executor["handler"], enabled=executor["enabled"], run=executor["run"],
            on_skip=executor["on_skip"], mode=executor["mode"],
        ),
        required=node["required"],
    )


def _parse_graph(value: Any) -> CrossExecutionGraph:
    wire = _expect_object(value, "graph", {"schema_version", "compile_identity", "nodes"})
    if wire["schema_version"] != CROSS_EXECUTION_GRAPH_SCHEMA_VERSION:
        raise CrossExecutionGraphStoreError(f"unknown graph schema version {wire['schema_version']!r}")
    identity = _expect_object(
        wire["compile_identity"], "compile_identity", {"schema_version", "fingerprint"},
    )
    if identity["schema_version"] != CROSS_EXECUTION_GRAPH_SCHEMA_VERSION:
        raise CrossExecutionGraphStoreError("unknown compile identity schema version")
    if not isinstance(wire["nodes"], list):
        raise CrossExecutionGraphStoreError("nodes must be a list")
    graph = CrossExecutionGraph(
        compile_identity=CrossExecutionGraphCompileIdentity(
            schema_version=identity["schema_version"],
            fingerprint=_expect_string(identity["fingerprint"], "compile_identity.fingerprint"),
        ),
        nodes=tuple(_parse_node(node, index) for index, node in enumerate(wire["nodes"])),
    )
    _validate_graph(graph)
    return graph


def _validate_graph(graph: CrossExecutionGraph) -> None:
    """Fail closed on malformed topology, executor assignment, or fingerprint."""
    if not isinstance(graph, CrossExecutionGraph):
        raise CrossExecutionGraphStoreError("graph must be a CrossExecutionGraph")
    if graph.compile_identity.schema_version != CROSS_EXECUTION_GRAPH_SCHEMA_VERSION:
        raise CrossExecutionGraphStoreError("unknown compile identity schema version")
    seen: set[str] = set()
    kinds: list[CrossExecutionGraphNodeKind] = []
    for index, node in enumerate(graph.nodes):
        if not isinstance(node.identity, str) or not node.identity or node.identity in seen:
            raise CrossExecutionGraphStoreError(f"invalid or duplicate node identity at nodes[{index}]")
        if not isinstance(node.dependencies, tuple) or len(set(node.dependencies)) != len(node.dependencies):
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has invalid dependencies")
        if any(dep not in seen for dep in node.dependencies):
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has dangling or non-topological dependency")
        if node.identity in node.dependencies:
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has self dependency")
        if not isinstance(node.kind, CrossExecutionGraphNodeKind) or not isinstance(node.owner, CrossExecutionGraphNodeOwner):
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has invalid kind or owner")
        if not isinstance(node.executor.executor, CrossExecutionGraphExecutor):
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has invalid executor")
        if not isinstance(node.required, bool) or not isinstance(node.executor.enabled, bool):
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has invalid boolean policy")
        assignment = {
            CrossExecutionGraphNodeKind.GLOBAL_PHASE: (CrossExecutionGraphNodeOwner.GLOBAL, CrossExecutionGraphExecutor.GLOBAL_HANDLER),
            CrossExecutionGraphNodeKind.PROJECT: (CrossExecutionGraphNodeOwner.PROJECT, CrossExecutionGraphExecutor.PROJECT_PIPELINE),
            CrossExecutionGraphNodeKind.CONTRACT_CHECK: (CrossExecutionGraphNodeOwner.RUNNER, CrossExecutionGraphExecutor.RUNNER_GATE),
            CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE: (CrossExecutionGraphNodeOwner.RUNNER, CrossExecutionGraphExecutor.RUNNER_GATE),
        }[node.kind]
        if (node.owner, node.executor.executor) != assignment:
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has invalid owner/executor assignment")
        if node.kind is CrossExecutionGraphNodeKind.GLOBAL_PHASE and not node.executor.handler:
            raise CrossExecutionGraphStoreError(f"nodes[{index}] global executor has no handler")
        if node.kind in {
            CrossExecutionGraphNodeKind.CONTRACT_CHECK,
            CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE,
        } and (
            node.executor.run not in {"always", "auto", "manual_confirm", "never"}
            or node.executor.on_skip not in {"block", "allow_with_gap", "allow"}
        ):
            raise CrossExecutionGraphStoreError(f"nodes[{index}] has invalid runner gate policy")
        seen.add(node.identity)
        kinds.append(node.kind)
    if kinds.count(CrossExecutionGraphNodeKind.CONTRACT_CHECK) != 1 or kinds.count(CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE) != 1:
        raise CrossExecutionGraphStoreError("graph must contain exactly one of each runner gate")
    contract_index = kinds.index(CrossExecutionGraphNodeKind.CONTRACT_CHECK)
    cfa_index = kinds.index(CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE)
    if (
        cfa_index != len(graph.nodes) - 1
        or cfa_index != contract_index + 1
        or graph.nodes[cfa_index].dependencies != (graph.nodes[contract_index].identity,)
    ):
        raise CrossExecutionGraphStoreError("cross final acceptance must directly depend on contract check")
    expected = _fingerprint(graph.nodes)
    if graph.compile_identity.fingerprint != expected:
        raise CrossExecutionGraphStoreError("compile fingerprint mismatch")


def load_cross_execution_graph(run_dir: Path) -> CrossExecutionGraph:
    """Load one strict graph artifact without mutating the run directory."""
    path = Path(run_dir) / CROSS_EXECUTION_GRAPH_FILENAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise CrossExecutionGraphStoreError(f"cross execution graph is missing: {path}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrossExecutionGraphStoreError(f"malformed cross execution graph: {path}") from exc
    return _parse_graph(value)


def write_cross_execution_graph(run_dir: Path, graph: CrossExecutionGraph) -> Path:
    """Create an immutable snapshot atomically, or confirm an equal snapshot."""
    target = Path(run_dir) / CROSS_EXECUTION_GRAPH_FILENAME
    payload = serialize_cross_execution_graph(graph)
    if target.exists():
        existing = load_cross_execution_graph(run_dir)
        if existing != graph:
            raise CrossExecutionGraphStoreError("existing cross execution graph differs from requested graph")
        return target
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{CROSS_EXECUTION_GRAPH_FILENAME}.", suffix=".tmp", dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise
    return target
