"""
core/contracts/cross_plan_schema.py — JSON schema for the cross architect's
CROSS-PLAN output (ADR 0054).

The cross architect emits one JSON object. That object is the machine ground
truth; the cross parser validates it against the schema below before any
subtask is routed, then Orcho renders human-readable ``cross_plan.md``
deterministically from the parsed object. This mirrors the mono PLAN contract
(:mod:`core.contracts.plan_schema`) — the cross architect now "speaks JSON"
just like the mono architect.

Dependency-free (no pydantic) so core stays importable on a bare stdlib
install. Validation lives in :func:`validate_cross_plan_dict`.

Fields:

* ``short_summary`` — compact headline for CLI / MCP / dashboards.
* ``interface_contract`` — the shared producer/consumer contract (field
  names, types, payloads, persisted shapes, endpoints). The *key* is always
  required; the *value* may be ``""`` only for a single-alias run (a
  coordinated change across >1 alias must name its shared surface).
* ``implementation_order`` — narrative ordering steps (descriptive, human
  facing; NOT a routing input — that is ``subtasks[].depends_on``).
* ``subtasks`` — one entry per supplied alias. ``depends_on`` carries the
  typed cross-alias dependency edges (the routing/ordering DATA; ADR 0057
  owns the topological-sort behavior that consumes them). ``produces`` /
  ``consumes`` are descriptive only.

Unlike :func:`core.contracts.plan_schema.validate_plan_dict`, the validator
takes the supplied ``aliases`` because alias coverage is task-specific: every
alias must receive exactly one subtask, and dependency edges must reference
declared aliases and stay acyclic.
"""
from __future__ import annotations

from typing import Any

CROSS_PLAN_SHORT_SUMMARY_MAX_CHARS = 280

CROSS_PLAN_REQUIRED_KEYS = (
    "short_summary",
    "interface_contract",
    "implementation_order",
    "subtasks",
)

SUBTASK_REQUIRED_KEYS = ("alias", "goal", "spec")
SUBTASK_OPTIONAL_KEYS = ("depends_on", "files", "produces", "consumes")

# Per-subtask fields that, when present, must be ``list[str]``.
_SUBTASK_LIST_OF_STR_FIELDS = ("depends_on", "files")
# Per-subtask fields that, when present, must be ``str`` (descriptive prose).
_SUBTASK_STR_FIELDS = ("produces", "consumes")


class CrossPlanSchemaError(ValueError):
    """Raised when a cross-plan dict does not match the expected schema."""


def validate_cross_plan_dict(data: Any, aliases: list[str]) -> dict[str, Any]:
    """Validate ``data`` against the cross-plan schema for ``aliases``.

    Returns the (normalized) dict on success. Raises
    :class:`CrossPlanSchemaError` on any structural defect, alias-coverage
    gap, dangling/self/cyclic ``depends_on`` edge, or missing shared
    ``interface_contract`` on a multi-alias run. A cyclic dependency graph is
    invalid *plan data* (mirrors mono ``validate_dag``); the topological-sort
    behavior that consumes the edges lives in ADR 0057, not here.
    """
    if not isinstance(data, dict):
        raise CrossPlanSchemaError(
            f"cross plan must be a JSON object, got {type(data).__name__}"
        )

    missing = [k for k in CROSS_PLAN_REQUIRED_KEYS if k not in data]
    if missing:
        raise CrossPlanSchemaError(f"cross plan missing required keys: {missing}")

    short_summary = data["short_summary"]
    if not isinstance(short_summary, str) or not short_summary.strip():
        raise CrossPlanSchemaError("short_summary must be a non-empty string")
    if len(short_summary) > CROSS_PLAN_SHORT_SUMMARY_MAX_CHARS:
        data["short_summary"] = (
            short_summary[: CROSS_PLAN_SHORT_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        )

    interface_contract = data["interface_contract"]
    if not isinstance(interface_contract, str):
        raise CrossPlanSchemaError("interface_contract must be a string")
    if len(aliases) > 1 and not interface_contract.strip():
        raise CrossPlanSchemaError(
            "interface_contract must be non-empty when more than one alias is "
            "involved — a coordinated change must name its shared surface"
        )

    order = data["implementation_order"]
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise CrossPlanSchemaError(
            "implementation_order must be a list of strings"
        )

    subtasks = data["subtasks"]
    if not isinstance(subtasks, list):
        raise CrossPlanSchemaError("subtasks must be a list")
    if not subtasks:
        raise CrossPlanSchemaError("subtasks list is empty")

    for i, st in enumerate(subtasks):
        _validate_subtask(st, i)

    _validate_alias_coverage(subtasks, aliases)
    _validate_dependency_edges(subtasks)

    return data


def _validate_subtask(st: Any, index: int) -> None:
    where = f"subtasks[{index}]"
    if not isinstance(st, dict):
        raise CrossPlanSchemaError(
            f"{where} must be an object, got {type(st).__name__}"
        )

    missing = [k for k in SUBTASK_REQUIRED_KEYS if k not in st]
    if missing:
        raise CrossPlanSchemaError(f"{where} missing required keys: {missing}")

    for k in SUBTASK_REQUIRED_KEYS:
        if not isinstance(st[k], str) or not st[k].strip():
            raise CrossPlanSchemaError(f"{where}.{k} must be a non-empty string")

    for k in _SUBTASK_LIST_OF_STR_FIELDS:
        if (
            k in st and st[k] is not None
            and (not isinstance(st[k], list) or not all(isinstance(x, str) for x in st[k]))
        ):
            raise CrossPlanSchemaError(f"{where}.{k} must be a list of strings")

    for k in _SUBTASK_STR_FIELDS:
        if k in st and st[k] is not None and not isinstance(st[k], str):
            raise CrossPlanSchemaError(f"{where}.{k} must be a string or null")


def _validate_alias_coverage(subtasks: list[Any], aliases: list[str]) -> None:
    """Exactly one subtask per supplied alias — no missing, extra, or dup.

    Structurally enforces the cross-validate-plan "alias coverage" rule on
    write, before the reviewer runs.
    """
    declared = list(aliases)
    seen: list[str] = [st["alias"] for st in subtasks]

    duplicates = sorted({a for a in seen if seen.count(a) > 1})
    if duplicates:
        raise CrossPlanSchemaError(f"duplicate subtask alias(es): {duplicates}")

    declared_set = set(declared)
    seen_set = set(seen)
    extra = sorted(seen_set - declared_set)
    if extra:
        raise CrossPlanSchemaError(
            f"subtask alias(es) not in the supplied aliases {declared}: {extra}"
        )
    missing = sorted(declared_set - seen_set)
    if missing:
        raise CrossPlanSchemaError(
            f"no subtask for supplied alias(es): {missing}"
        )


def _validate_dependency_edges(subtasks: list[Any]) -> None:
    """Closed-ref + no-self-edge + acyclic check over ``depends_on``.

    A cyclic or dangling dependency graph is invalid plan data — reject it
    here so an unexecutable typed plan is never persisted. Mirrors mono
    ``pipeline.plan_parser.validate_dag`` (Kahn's algorithm).
    """
    aliases = [st["alias"] for st in subtasks]
    alias_set = set(aliases)
    deps: dict[str, list[str]] = {}
    for st in subtasks:
        alias = st["alias"]
        edges = list(st.get("depends_on") or ())
        unknown = [d for d in edges if d not in alias_set]
        if unknown:
            raise CrossPlanSchemaError(
                f"subtask '{alias}' depends_on unknown alias(es): {unknown}"
            )
        if alias in edges:
            raise CrossPlanSchemaError(f"subtask '{alias}' depends on itself")
        deps[alias] = edges

    # Kahn's algorithm — a fully-drained queue means the graph is acyclic.
    indeg = {a: 0 for a in aliases}
    out: dict[str, list[str]] = {a: [] for a in aliases}
    for alias, edges in deps.items():
        for d in edges:
            indeg[alias] += 1
            out[d].append(alias)

    queue = [a for a, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for nxt in out[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if visited != len(aliases):
        unresolved = sorted(a for a, d in indeg.items() if d > 0)
        raise CrossPlanSchemaError(
            f"cross plan contains a dependency cycle involving: {unresolved}"
        )


def cross_plan_alias_map(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the ``{alias: subtask_dict}`` map from a validated cross plan.

    Convenience for callers that route per alias (subtask distribution,
    handoff slicing) without re-iterating the list.
    """
    return {st["alias"]: st for st in data["subtasks"]}


# Human-readable schema embedded into the CROSS-PLAN prompt so the architect
# knows what shape to emit.
CROSS_PLAN_SCHEMA_DOC = """
Emit exactly one JSON object with this shape:

{
  "short_summary": "<one or two sentences, target 280 chars; CLI/MCP headline>",
  "interface_contract": "<shared producer/consumer contract: field names, types, payloads, persisted shapes (DB columns, file formats), API endpoint paths and response fields>",
  "implementation_order": ["<narrative step: which repo changes first and what to verify>"],
  "subtasks": [
    {
      "alias": "<must match a supplied alias exactly>",
      "goal": "<one-sentence outcome for this repo>",
      "spec": "<detailed instructions for the child implementer>",
      "depends_on": ["<alias this subtask requires to land first>"],
      "files": ["[alias]/relative/path"],
      "produces": "<what this repo gives the others>",
      "consumes": "<what this repo takes from the others>"
    }
  ]
}

Rules:
- Required: `short_summary`, `interface_contract`, `implementation_order`, `subtasks`.
- Keep `short_summary` <=280 chars.
- Emit exactly one subtask per supplied alias — no missing, extra, or duplicate alias.
- `interface_contract` must be non-empty when more than one alias is involved; it may be "" only for a single-alias run.
- `implementation_order` is an array of strings (producer/schema first, then consumers, then derived views). Use [] for a single-repo change.
- `depends_on` lists the aliases this subtask requires first; entries must be declared aliases, never the subtask's own alias, and the dependency graph must be acyclic. Use [] when there is no cross-alias dependency.
- `files` use the `[alias]/relative/path` form; tool calls still take absolute paths.
- `produces` / `consumes` are descriptive prose, not routing inputs.
- No prose, markdown fence, or trailing commentary — the JSON object is the only output.
""".strip()
