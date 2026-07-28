"""Plan-record projection for mono and canonical cross-project runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.contracts.cross_plan_schema import validate_cross_plan_dict


def build_plan_record(
    run_dir: Path,
    *,
    meta: dict[str, Any],
    run_start_payload: dict[str, Any] | None,
    mono_plan_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the mono plan event, or a validated canonical cross plan.

    ``plan.parsed`` remains authoritative whenever it exists.  Cross runs
    have no equivalent mono event, so their canonical artifact is a fallback
    only; malformed or incomplete durable state deliberately looks absent.
    """
    if mono_plan_payload is not None:
        return _build_mono_plan_record(mono_plan_payload)

    aliases = _declared_aliases(meta, run_start_payload)
    if aliases is None:
        return _absent_plan_record()

    try:
        artifact = json.loads((run_dir / "cross_plan.json").read_text(
            encoding="utf-8",
        ))
        plan = validate_cross_plan_dict(artifact, aliases)
    except (OSError, json.JSONDecodeError, ValueError):
        return _absent_plan_record()

    return {
        "source": "json",
        "short_summary": plan["short_summary"],
        "planning_context": plan["interface_contract"],
        "subtask_count": len(plan["subtasks"]),
        "has_contract": bool(plan["interface_contract"].strip()),
        "goal": None,
        "acceptance_criteria": [],
        "owned_files": [],
        "commands_to_run": [],
        "risks": [],
        "review_focus": [],
        "mcp_context": [],
        "subtasks": [],
    }


def _declared_aliases(
    meta: dict[str, Any], run_start_payload: dict[str, Any] | None,
) -> list[str] | None:
    """Return declared project aliases in durable insertion order."""
    projects = meta.get("projects")
    if not isinstance(projects, dict) or not projects:
        projects = (run_start_payload or {}).get("projects")
    if not isinstance(projects, dict) or not projects:
        return None
    aliases = list(projects)
    return aliases if all(isinstance(alias, str) and alias for alias in aliases) else None


def _absent_plan_record() -> dict[str, Any]:
    return {
        "source": "absent",
        "short_summary": "",
        "planning_context": "",
        "subtask_count": 0,
        "has_contract": False,
        "goal": None,
        "acceptance_criteria": [],
        "owned_files": [],
        "commands_to_run": [],
        "risks": [],
        "review_focus": [],
        "mcp_context": [],
        "subtasks": [],
    }


def _build_mono_plan_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve the established ``plan.parsed`` event projection exactly."""
    return {
        "source": str(payload.get("source", "json")),
        "short_summary": str(payload.get("short_summary") or ""),
        "planning_context": str(payload.get("planning_context") or ""),
        "subtask_count": int(payload.get("subtask_count", 0)),
        "has_contract": bool(payload.get("has_contract", False)),
        "goal": payload.get("goal") or None,
        "acceptance_criteria": _string_list_from_payload(
            payload, "acceptance_criteria", "acceptance_criteria_count",
        ),
        "owned_files": _string_list_from_payload(
            payload, "owned_files", "owned_files_count",
        ),
        "commands_to_run": _string_list_from_payload(
            payload, "commands_to_run", "commands_to_run_count",
        ),
        "risks": _string_list_from_payload(payload, "risks"),
        "review_focus": _string_list_from_payload(payload, "review_focus"),
        "mcp_context": [
            dict(x) for x in payload.get("mcp_context", []) if isinstance(x, dict)
        ],
        "subtasks": [
            dict(x) for x in payload.get("subtasks", []) if isinstance(x, dict)
        ],
    }


def _string_list_from_payload(
    payload: dict[str, Any], key: str, count_key: str | None = None,
) -> list[str]:
    """Return a typed contract list from a ``plan.parsed`` payload."""
    value = payload.get(key)
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, str)]
    if count_key is None:
        return []
    n = int(payload.get(count_key, 0) or 0)
    return [f"<entry {i + 1}>" for i in range(n)]
