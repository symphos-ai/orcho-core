"""MCP-alignment smoke for the ``allowed_modifications`` plan field.

This test pins the **exact** surface that the ``orcho_plan_validate`` MCP
tool exercises, proving the additive ``allowed_modifications`` plan field
needs **zero** changes in orcho-mcp.

Contract binding (read-only evidence gathered during planning):

* ``orcho-mcp/src/orcho_mcp/authoring/plan_validation.py`` —
  ``validate_plan_document`` (the sync function backing the
  ``orcho_plan_validate`` tool) wraps ``pipeline.plan_parser.parse_plan``
  and, at **line 53**, lazily imports ``core.contracts.plan_schema``
  (``PlanSchemaError``). It owns **no copy** of the plan schema; it
  delegates validation entirely to orcho-core.
* Its ``SubTaskRecord`` projection
  (``orcho-mcp/src/orcho_mcp/schemas/authoring.py``) lists only
  ``id / goal / spec / files / skill / model / depends_on /
  done_criteria`` — it does **not** surface per-task ``owned_files``
  today, and therefore does not surface per-task
  ``allowed_modifications`` either. The new field is validated by the
  same core schema and carried on core's ``SubTask``; orcho-mcp simply
  does not project it onto its narrower wire record (exactly as it omits
  ``owned_files``), so parity with ``owned_files`` means "zero edits to
  orcho-mcp" — the MCP record neither re-declares the field nor rejects
  the new key.

The smoke runs plan markdown carrying ``allowed_modifications`` at the
plan level **and** the per-task level through precisely
``parse_plan`` + ``core.contracts.plan_schema`` validation — the same
two calls ``validate_plan_document`` makes — and asserts the field is
accepted, the plan-level value reaches :class:`ParsedPlan`, plans
without the field still validate (parity), and a malformed type is
rejected with :class:`PlanSchemaError`.
"""
from __future__ import annotations

import json

import pytest

from core.contracts.plan_schema import PlanSchemaError, validate_plan_dict
from pipeline.plan_parser import parse_plan


def _plan_markdown(**plan_overrides: object) -> str:
    """Build a json-fenced plan document — the markdown shape an MCP
    caller passes to ``orcho_plan_validate``."""
    plan: dict = {
        "short_summary": "Bump a dependency and accept its lockfile churn.",
        "planning_context": "package.json change regenerates package-lock.json.",
        "tasks": [
            {"id": "t1", "goal": "Update the dependency"},
        ],
    }
    plan.update(plan_overrides)
    return f"# Plan\n\n```json\n{json.dumps(plan)}\n```\n"


class TestPlanValidateMcpSurface:
    def test_plan_with_allowed_modifications_both_levels_accepted(self) -> None:
        # Plan-level + per-task allowed_modifications. This is the exact
        # surface orcho_plan_validate runs (parse_plan → plan_schema).
        text = _plan_markdown(
            allowed_modifications=["package-lock.json — derived from package.json"],
            tasks=[
                {
                    "id": "t1",
                    "goal": "Update the dependency",
                    "allowed_modifications": ["yarn.lock — derived"],
                },
            ],
        )

        plan = parse_plan(text)  # must not raise — the field is accepted

        # Plan-level value propagates onto ParsedPlan.
        assert plan.allowed_modifications == (
            "package-lock.json — derived from package.json",
        )
        # The per-task value propagates onto the parsed SubTask through the
        # real JSON path (not just schema-accepted). The MCP SubTaskRecord
        # projection drops it the same way it drops per-task owned_files,
        # which is exactly why orcho-mcp needs no change.
        assert len(plan.subtasks) == 1
        assert plan.subtasks[0].allowed_modifications == ("yarn.lock — derived",)

    def test_per_task_field_renders_in_plan_contract_via_parse_plan(self) -> None:
        # End-to-end on the fresh JSON parse path: a per-task-only plan
        # (empty top-level) parsed by parse_plan must surface the entry,
        # tagged with its task id, in the rendered Plan Contract — the same
        # block validate_plan / review gates read.
        from pipeline.plan_contract import render_plan_contract

        text = _plan_markdown(
            tasks=[
                {
                    "id": "t9",
                    "goal": "Update the dependency",
                    "allowed_modifications": ["yarn.lock — derived"],
                },
            ],
        )

        plan = parse_plan(text)

        assert plan.subtasks[0].allowed_modifications == ("yarn.lock — derived",)
        # has_contract is true for a per-task-only plan, so the block renders.
        assert plan.has_contract is True
        contract = render_plan_contract(plan)
        assert "## Plan Contract" in contract
        assert "**Allowed companion modifications:**" in contract
        assert "- [t9] yarn.lock — derived" in contract

    def test_schema_validates_per_task_allowed_modifications(self) -> None:
        # The precise per-task contract orcho_plan_validate delegates to:
        # core.contracts.plan_schema accepts a per-task list[str] field.
        data = {
            "short_summary": "x",
            "planning_context": "x",
            "tasks": [
                {
                    "id": "t1",
                    "goal": "y",
                    "allowed_modifications": ["a.lock — derived", "b.snap — golden"],
                },
            ],
        }
        # Returns the dict on success; raises on failure.
        assert validate_plan_dict(dict(data))["tasks"][0]["allowed_modifications"] == [
            "a.lock — derived",
            "b.snap — golden",
        ]

    def test_plan_without_field_accepted_parity(self) -> None:
        plan = parse_plan(_plan_markdown())
        assert plan.allowed_modifications == ()
        assert plan.subtasks[0].allowed_modifications == ()

    def test_plan_level_invalid_type_rejected(self) -> None:
        # Bare string instead of list[str] — rejected, never coerced.
        text = _plan_markdown(allowed_modifications="not-a-list")
        with pytest.raises(PlanSchemaError, match="allowed_modifications"):
            parse_plan(text)

    def test_per_task_invalid_type_rejected(self) -> None:
        text = _plan_markdown(
            tasks=[
                {
                    "id": "t1",
                    "goal": "y",
                    "allowed_modifications": ["ok.lock", 42],
                },
            ],
        )
        with pytest.raises(PlanSchemaError, match="allowed_modifications"):
            parse_plan(text)
