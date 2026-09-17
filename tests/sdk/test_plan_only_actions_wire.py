"""Совместимость plan-only действий с SDK status и MCP pass-through.

MCP services.run_reads передаёт Action.to_dict() в NextActionRecord.
Проверяем эту core-owned границу без зависимости core от orcho-mcp.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import load_status, to_jsonable
from sdk.actions import Action

pytestmark = pytest.mark.mcp_integration


@pytest.mark.parametrize("has_plan", [True, False])
def test_plan_only_status_action_wire(tmp_path: Path, has_plan: bool) -> None:
    run_id = "20260917_plan_only"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    task = "Implement the approved logging plan"
    (run_dir / "meta.json").write_text(json.dumps({
        "status": "done", "task": task, "plan_source": "local",
        "commit_delivery": {"status": "not_applicable", "action": "none"},
    }))
    if has_plan:
        (run_dir / "parsed_plan.json").write_text(json.dumps({
            "short_summary": "Logging", "planning_context": "Keep logs structured",
            "tasks": [{"id": "t1", "goal": task}],
        }))

    status = load_status(run_id, runs_dir=tmp_path, cwd=None)
    payload = json.loads(json.dumps(to_jsonable(status)))
    assert payload["meta"]["status"] == "done"
    if not has_plan:
        assert status.next_actions == ()
        assert payload["next_actions"] == []
        return

    action, = status.next_actions
    wire = json.loads(json.dumps(action.to_dict()))
    assert set(wire) == {"intent", "tool", "args", "optional"}
    assert wire["tool"] == "orcho_run_start"
    assert wire["args"] == {"from_run_plan": run_id, "profile": "feature", "task": task}
    assert wire["optional"] is True
    assert Action(**wire) == action
    public_action, = payload["next_actions"]
    assert all(public_action[key] == value for key, value in wire.items())
    assert public_action["kind"] == "ready_call"
    assert public_action["requires_operator_input"] is False
