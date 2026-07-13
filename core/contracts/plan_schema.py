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
* ``commands_to_run`` — targeted verification commands (tests, linters);
  full or broad suites are project gate-policy, not implement actions
* ``risks`` — invariants the agent must not violate
* ``review_focus`` — what the reviewer should pay attention to
* ``mcp_context`` — pre-fetched external context (REA-5 fills this in)

All seven REA-1 fields are **optional**. When present, types are validated;
malformed fields fail the plan with a useful error before implement runs.
"""
from __future__ import annotations

from typing import Any

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
_PLAN_LIST_OF_STR_FIELDS = (
    "acceptance_criteria",
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


# Human-readable schema description embedded into the PLAN prompt so the
# architect knows what shape to emit.
PLAN_SCHEMA_DOC = """
Emit exactly one JSON object with this shape:

{
  "short_summary": "<one or two sentences, target 280 chars>",
  "planning_context": "<why this plan has this shape; discovery notes, constraints, current state>",

  "goal": "<one-sentence machine-readable target>",
  "acceptance_criteria": ["<checkable condition>"],
  "owned_files": ["path/to/file"],
  "allowed_modifications": ["<glob — reason; companion change allowed in any task>"],
  "commands_to_run": ["<targeted command that verifies this change>"],
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
      "allowed_modifications": ["<companion change allowed for THIS task beyond the project list>"]
    }
  ]
}

Rules:
- Keys are literal protocol identifiers: copy every field name above verbatim in English; never translate, localize, or rename a key. Only string values may be written in another language.
- Required: `short_summary`, `planning_context`, `tasks`; never emit `plan_summary`.
- Keep `short_summary` <=280 chars and put discovery/constraints in `planning_context`.
- Optional list fields are arrays of strings; `mcp_context` is a list of objects.
- `commands_to_run` contains only targeted commands for the concrete change.
  The project's full or broad suite is gate-policy, not an implement action.
- `allowed_modifications` (top-level and per-task) lists companion changes allowed beyond the owned files — lockfiles, regenerated snapshots, derived artifacts — that a reviewer must not treat as a scope violation; their content is still reviewed.
- Task ids are unique; `depends_on` references known ids only; dependency graph is acyclic.
- Use [] for empty lists and null for absent optional `skill` / `model`.
- Tasks without dependencies are roots; unrelated DAG branches may run in parallel.
""".strip()
