"""
core/contracts/plan_schema.py — JSON schema for the team-lead PLAN output.

The architect emits one JSON object. That object is the machine ground truth;
the parser validates it against the schema below before implement runs. Orcho then
renders human-readable plan markdown deterministically from the parsed object.

Schema is kept dependency-free (no pydantic) so the core stays importable on
a bare stdlib install. Validation lives in :func:`validate_plan_dict`.

REA-3.5.1 names the cognitive and context fields explicitly:

* ``short_summary`` — compact headline for CLI / MCP / dashboards
* ``planning_context`` — discovery notes and why the plan has this shape

REA-1 layered on top of the original DAG contract a small **typed plan
contract** at the top level:

* ``goal`` — single-sentence machine-readable target
* ``acceptance_criteria`` — list of checkable conditions
* ``owned_files`` — files in the plan's write scope
* ``commands_to_run`` — verification commands (tests, linters)
* ``risks`` — invariants the agent must not violate
* ``review_focus`` — what the reviewer should pay attention to
* ``mcp_context`` — pre-fetched external context (REA-5 fills this in)

All seven REA-1 fields are **optional**. When present, types are validated;
malformed fields fail the plan with a useful error before implement runs.
"""
from __future__ import annotations

from typing import Any

from core.contracts.criteria import (
    AcceptanceCriterion,
    CriterionSchemaError,
    coerce_acceptance_criteria,
    criteria_to_wire,
    validate_acceptance_refs,
)

PLAN_SHORT_SUMMARY_MAX_CHARS = 280

# Top-level keys expected in the JSON plan object.
PLAN_REQUIRED_KEYS = ("short_summary", "planning_context", "tasks")
PLAN_OPTIONAL_KEYS = (
    "acceptance_criteria",
    # REA-1 typed plan contract fields:
    "goal",
    "owned_files",
    "allowed_modifications",
    "commands_to_run",
    "risks",
    "review_focus",
    "mcp_context",
)

# Top-level fields that, when present, must be ``list[str]``.
# ``acceptance_criteria`` is deliberately absent: since ADR 0188 it is a list
# of typed criterion objects, validated by
# :mod:`core.contracts.criteria` (which also owns the single legacy
# ``list[str]`` ingress normalizer).
_PLAN_LIST_OF_STR_FIELDS = (
    "owned_files",
    "allowed_modifications",
    "commands_to_run",
    "risks",
    "review_focus",
)

# Keys per task entry.
TASK_REQUIRED_KEYS = ("id", "goal")
TASK_OPTIONAL_KEYS = (
    "spec",
    "files",
    "skill",
    "model",
    "depends_on",
    "done_criteria",
    # ADR 0188: references to plan-level criterion IDs, never copies of the
    # criterion body.
    "acceptance_refs",
    # Additive subtask fields the SubTask dataclass already carries.
    # Validating them here keeps the durable parsed_plan.json artefact
    # (which serialises every SubTask field) honest end-to-end — see
    # ``pipeline.plan_artifacts`` for the load-side hard-fail policy.
    "owned_files",
    "allowed_modifications",
    "architectural_decision",
)

# Per-task fields that, when present, must be ``list[str]``. Validated
# uniformly in :func:`_validate_task`. ``files`` / ``depends_on`` /
# ``done_criteria`` predate REA-1; ``owned_files`` was added so the
# parsed_plan.json round trip cannot silently coerce a bare string into
# a tuple of characters (``"abc"`` → ``("a", "b", "c")``).
# ``allowed_modifications`` mirrors ``owned_files``: a per-task list of
# companion files the reviewer may accept beyond the project-wide list.
_TASK_LIST_OF_STR_FIELDS = (
    "files",
    "depends_on",
    "done_criteria",
    "owned_files",
    "allowed_modifications",
)


class PlanSchemaError(ValueError):
    """Raised when a plan dict does not match the expected schema."""


def validate_plan_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the plan schema. Returns the dict on success.

    Checks the structural contract only — DAG semantics (cycles, dangling
    refs, duplicate ids) are validated separately by ``plan_parser`` so
    callers can format richer error messages with task context.
    """
    if not isinstance(data, dict):
        raise PlanSchemaError(f"plan must be a JSON object, got {type(data).__name__}")

    missing = [k for k in PLAN_REQUIRED_KEYS if k not in data]
    if missing:
        raise PlanSchemaError(f"plan missing required keys: {missing}")

    if "plan_summary" in data:
        raise PlanSchemaError(
            "plan_summary is not part of the PLAN contract; "
            "use short_summary and planning_context"
        )

    short_summary = data["short_summary"]
    if not isinstance(short_summary, str) or not short_summary.strip():
        raise PlanSchemaError("short_summary must be a non-empty string")
    if len(short_summary) > PLAN_SHORT_SUMMARY_MAX_CHARS:
        data["short_summary"] = (
            short_summary[: PLAN_SHORT_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        )

    if not isinstance(data["planning_context"], str) or not data["planning_context"].strip():
        raise PlanSchemaError("planning_context must be a non-empty string")

    tasks = data["tasks"]
    if not isinstance(tasks, list):
        raise PlanSchemaError("tasks must be a list")
    if not tasks:
        raise PlanSchemaError("tasks list is empty")

    for i, t in enumerate(tasks):
        _validate_task(t, i)

    _validate_plan_contract(data)

    return data


def _validate_task(t: Any, index: int) -> None:
    where = f"tasks[{index}]"
    if not isinstance(t, dict):
        raise PlanSchemaError(f"{where} must be an object, got {type(t).__name__}")

    missing = [k for k in TASK_REQUIRED_KEYS if k not in t]
    if missing:
        raise PlanSchemaError(f"{where} missing required keys: {missing}")

    if not isinstance(t["id"], str) or not t["id"].strip():
        raise PlanSchemaError(f"{where}.id must be a non-empty string")
    if not isinstance(t["goal"], str) or not t["goal"].strip():
        raise PlanSchemaError(f"{where}.goal must be a non-empty string")

    for k in ("spec", "skill", "model"):
        if k in t and t[k] is not None and not isinstance(t[k], str):
            raise PlanSchemaError(f"{where}.{k} must be a string or null")

    for k in _TASK_LIST_OF_STR_FIELDS:
        if (k in t and t[k] is not None and
                (not isinstance(t[k], list) or not all(isinstance(x, str) for x in t[k]))):
            raise PlanSchemaError(f"{where}.{k} must be a list of strings")

    try:
        validate_acceptance_refs(
            t.get("acceptance_refs"), where=f"{where}.acceptance_refs",
        )
    except CriterionSchemaError as e:
        raise PlanSchemaError(str(e)) from e

    # ``architectural_decision`` is a strict bool (no truthy-coercion
    # of e.g. ``"false"`` → ``True``). The reader in plan_artifacts
    # used to call ``bool(...)`` on whatever came through, which would
    # silently promote any non-empty string. Validate strictly here so
    # the artefact loader's hard-fail invariant holds: an unreadable
    # field is rejected, never coerced.
    if (
        "architectural_decision" in t
        and t["architectural_decision"] is not None
        and not isinstance(t["architectural_decision"], bool)
    ):
        raise PlanSchemaError(
            f"{where}.architectural_decision must be a boolean",
        )


def _validate_plan_contract(data: dict[str, Any]) -> None:
    """Validate REA-1 typed-contract fields when present.

    All contract fields are optional — absent fields skip validation so
    pre-REA-1 plans continue to parse cleanly. Present fields must match
    their declared types; mismatches raise :class:`PlanSchemaError` so the
    plan is rejected before implement runs.
    """
    if ("goal" in data and data["goal"] is not None and
            (not isinstance(data["goal"], str) or not data["goal"].strip())):
        raise PlanSchemaError("goal must be a non-empty string")

    for key in _PLAN_LIST_OF_STR_FIELDS:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise PlanSchemaError(f"{key} must be a list of strings")

    if "mcp_context" in data and data["mcp_context"] is not None:
        ctx = data["mcp_context"]
        if not isinstance(ctx, list) or not all(isinstance(x, dict) for x in ctx):
            raise PlanSchemaError("mcp_context must be a list of objects")

    _validate_acceptance_contract(data)


def _validate_acceptance_contract(data: dict[str, Any]) -> None:
    """Validate typed criteria, task references, and executor coverage (ADR 0188).

    Legacy ``list[str]`` payloads are routed through the single ingress
    normalizer and rewritten in place to the typed wire shape, so every
    downstream reader of a validated plan dict sees exactly one form.
    """
    try:
        criteria = coerce_acceptance_criteria(data.get("acceptance_criteria"))
    except CriterionSchemaError as e:
        raise PlanSchemaError(str(e)) from e

    if "acceptance_criteria" in data and data["acceptance_criteria"] is not None:
        data["acceptance_criteria"] = criteria_to_wire(criteria)

    known = {c.id: c for c in criteria}
    referenced: set[str] = set()
    for i, task in enumerate(data["tasks"]):
        for ref in task.get("acceptance_refs") or ():
            ref = str(ref).strip()
            if ref not in known:
                raise PlanSchemaError(
                    f"tasks[{i}].acceptance_refs references unknown criterion "
                    f"{ref!r}; known ids: {sorted(known)}"
                )
            referenced.add(ref)

    unowned = [
        c.id
        for c in criteria
        if c.verify == "executable" and c.id not in referenced
    ]
    if unowned:
        raise PlanSchemaError(
            "every executable acceptance criterion needs at least one task "
            f"reference; unowned: {unowned}"
        )


def plan_acceptance_criteria(data: dict[str, Any]) -> tuple[AcceptanceCriterion, ...]:
    """Typed criteria for a plan dict, routing legacy payloads through ingress."""
    try:
        return coerce_acceptance_criteria(data.get("acceptance_criteria"))
    except CriterionSchemaError as e:
        raise PlanSchemaError(str(e)) from e


# Human-readable schema description embedded into the PLAN prompt so the
# architect knows what shape to emit.
PLAN_SCHEMA_DOC = """
Emit exactly one JSON object with this shape:

{
  "short_summary": "<one or two sentences, target 280 chars>",
  "planning_context": "<why this plan has this shape; discovery notes, constraints, current state>",

  "goal": "<one-sentence machine-readable target>",
  "acceptance_criteria": [
    {"id": "C1", "intent": "<checkable condition>", "verify": "executable",
     "gate_refs": [{"command": "<declared gate command>", "hook": "after_phase", "phase": "implement"}]},
    {"id": "C2", "intent": "<condition an agent can only inspect>", "verify": "agent_assertion"},
    {"id": "C3", "intent": "<condition only an operator can judge>", "verify": "human",
     "human_instructions": "<what the operator should exercise and record>"}
  ],
  "owned_files": ["path/to/file"],
  "allowed_modifications": ["<glob — reason; companion change allowed in any task>"],
  "commands_to_run": ["<command that verifies the change in this project>"],
  "risks": ["<invariant the agent must not violate>"],
  "review_focus": ["<what the reviewer should check>"],
  "mcp_context": [],

  "tasks": [
    {
      "id": "<short stable id, e.g. 'add-endpoint' or 'T1'>",
      "goal": "<one-sentence outcome>",
      "spec": "<detailed instructions for the executing agent>",
      "files": ["path/to/file1", "path/to/file2"],
      "skill": "<optional skill name from the registry, or null>",
      "model": "<optional model override, or null>",
      "depends_on": ["<id of another task>"],
      "done_criteria": ["<checkable condition>"],
      "acceptance_refs": ["C1"],
      "allowed_modifications": ["<companion change allowed for THIS task beyond the project list>"]
    }
  ]
}

Rules:
- Keys are literal protocol identifiers: copy every field name above verbatim in English; never translate, localize, or rename a key. Only string values may be written in another language.
- Required: `short_summary`, `planning_context`, `tasks`; never emit `plan_summary`.
- Keep `short_summary` <=280 chars and put discovery/constraints in `planning_context`.
- Optional list fields are arrays of strings; `mcp_context` is a list of objects.
- `acceptance_criteria` is a list of typed criterion objects, never a list of strings. Each has a unique `id` matching `C1`, `C2`, ... , a one-sentence `intent`, and exactly one `verify` class:
  - `executable` — requires a non-empty `gate_refs`; each ref is the COMPLETE scheduled gate identity `{"command", "hook", "phase"}` naming a gate the project's verification contract already declares and selects. A command name alone, an unknown gate, or raw shell text is invalid. Never turn `commands_to_run` into a gate ref.
  - `agent_assertion` — no `gate_refs`, no `human_instructions`; advisory evidence only, it can never prove a blocking condition.
  - `human` — non-empty `human_instructions`, no `gate_refs`; stays pending until an operator records a decision.
- `acceptance_refs` on a task lists plan criterion ids only; never copy or restate the criterion text. Every `executable` criterion must be referenced by at least one task.
- `allowed_modifications` (top-level and per-task) lists companion changes allowed beyond the owned files — lockfiles, regenerated snapshots, derived artifacts — that a reviewer must not treat as a scope violation; their content is still reviewed.
- Task ids are unique; `depends_on` references known ids only; dependency graph is acyclic.
- Use [] for empty lists and null for absent optional `skill` / `model`.
- Tasks without dependencies are roots; unrelated DAG branches may run in parallel.
""".strip()
