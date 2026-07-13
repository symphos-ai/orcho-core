"""REA-1 typed plan contract coverage.

Three layers:

1. ``core.contracts.plan_schema`` — new optional fields validated when
 present, plans without them still parse (backcompat).
2. ``pipeline.plan_parser.ParsedPlan`` — new fields populated from JSON
 fence.
3. ``pipeline.plan_contract.render_plan_contract`` — markdown rendering
 used by BUILD / REVIEW / FIX / FINAL_ACCEPTANCE prompts.
"""
from __future__ import annotations

import json

import pytest

from core.contracts.plan_schema import PLAN_SCHEMA_DOC, PlanSchemaError, validate_plan_dict
from pipeline.plan_contract import render_plan_contract
from pipeline.plan_parser import ParsedPlan, parse_plan

# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


def _minimal_plan() -> dict:
    """Smallest valid plan — required keys only, no contract fields."""
    return {
        "short_summary": "short summary",
        "planning_context": "planning context",
        "tasks": [{"id": "t1", "goal": "do thing"}],
    }


class TestSchemaBackcompat:
    """Plans authored before REA-1 must still validate cleanly."""

    def test_minimal_plan_valid_without_contract_fields(self) -> None:
        validate_plan_dict(_minimal_plan())

    def test_optional_acceptance_criteria_still_works(self) -> None:
        plan = _minimal_plan() | {"acceptance_criteria": ["a", "b"]}
        validate_plan_dict(plan)

    def test_old_plan_summary_field_is_rejected(self) -> None:
        plan = _minimal_plan() | {"plan_summary": "old ambiguous field"}
        with pytest.raises(PlanSchemaError, match="plan_summary"):
            validate_plan_dict(plan)


class TestPlanSchemaAuthoringGuidance:
    def test_commands_to_run_is_targeted_only(self) -> None:
        assert "targeted command that verifies this change" in PLAN_SCHEMA_DOC
        assert "full or broad suite is gate-policy" in PLAN_SCHEMA_DOC
        assert "not an implement action" in PLAN_SCHEMA_DOC


class TestSchemaContractFields:
    """REA-1 contract fields validate when present; type errors fail fast."""

    @pytest.mark.parametrize(
        "field, valid_value",
        [
            ("goal", "Fix it"),
            ("acceptance_criteria", ["AC1", "AC2"]),
            ("owned_files", ["a.py"]),
            ("allowed_modifications", ["package-lock.json — derived"]),
            ("commands_to_run", ["pytest -q"]),
            ("risks", ["don't change schema"]),
            ("review_focus", ["coverage"]),
            ("mcp_context", []),
        ],
    )
    def test_valid_typed_field_accepted(
        self, field: str, valid_value: object,
    ) -> None:
        validate_plan_dict(_minimal_plan() | {field: valid_value})

    def test_goal_must_be_non_empty_string(self) -> None:
        with pytest.raises(PlanSchemaError, match="goal"):
            validate_plan_dict(_minimal_plan() | {"goal": ""})

    def test_goal_must_be_string_not_list(self) -> None:
        with pytest.raises(PlanSchemaError, match="goal"):
            validate_plan_dict(_minimal_plan() | {"goal": ["wrong"]})

    @pytest.mark.parametrize(
        "field",
        [
            "acceptance_criteria",
            "owned_files",
            "allowed_modifications",
            "commands_to_run",
            "risks",
            "review_focus",
        ],
    )
    def test_list_of_str_fields_reject_non_list(self, field: str) -> None:
        with pytest.raises(PlanSchemaError, match=field):
            validate_plan_dict(_minimal_plan() | {field: "not a list"})

    @pytest.mark.parametrize(
        "field",
        [
            "acceptance_criteria",
            "owned_files",
            "allowed_modifications",
            "commands_to_run",
            "risks",
            "review_focus",
        ],
    )
    def test_list_of_str_fields_reject_non_string_items(self, field: str) -> None:
        with pytest.raises(PlanSchemaError, match=field):
            validate_plan_dict(_minimal_plan() | {field: [1, 2, 3]})

    def test_mcp_context_must_be_list_of_objects(self) -> None:
        with pytest.raises(PlanSchemaError, match="mcp_context"):
            validate_plan_dict(_minimal_plan() | {"mcp_context": ["not-a-dict"]})

    def test_null_contract_fields_treated_as_absent(self) -> None:
        """``None`` is permitted (architect can emit explicit nulls)."""
        plan = _minimal_plan() | {
            "goal": None,
            "acceptance_criteria": None,
            "owned_files": None,
            "commands_to_run": None,
            "risks": None,
            "review_focus": None,
            "mcp_context": None,
        }
        validate_plan_dict(plan)


# ─────────────────────────────────────────────────────────────────────────────
# Parser propagation into ParsedPlan
# ─────────────────────────────────────────────────────────────────────────────


def _plan_text_with_contract() -> str:
    plan_json = {
        "short_summary": "Fix validation rejection.",
        "planning_context": "Fix the validation bug",
        "goal": "Reject invalid payloads with 400",
        "acceptance_criteria": ["Invalid → 400", "Valid still 200"],
        "owned_files": ["app/validation.py"],
        "commands_to_run": ["pytest tests/ -q"],
        "risks": ["Do not change response schema"],
        "review_focus": ["Input validation coverage"],
        "mcp_context": [],
        "tasks": [
            {"id": "repair_changes", "goal": "Apply fix"},
        ],
    }
    return f"# Plan\n\n```json\n{json.dumps(plan_json)}\n```\n"


class TestParserPopulatesContract:
    def test_full_contract_propagates_to_parsed_plan(self) -> None:
        plan = parse_plan(_plan_text_with_contract())

        assert plan.goal == "Reject invalid payloads with 400"
        assert plan.acceptance_criteria == (
            "Invalid → 400", "Valid still 200",
        )
        assert plan.owned_files == ("app/validation.py",)
        assert plan.commands_to_run == ("pytest tests/ -q",)
        assert plan.risks == ("Do not change response schema",)
        assert plan.review_focus == ("Input validation coverage",)
        assert plan.mcp_context == ()
        assert plan.has_contract is True

    def test_allowed_modifications_propagate_both_levels(self) -> None:
        plan_json = {
            "short_summary": "Bump dep.",
            "planning_context": "lockfile churn expected",
            "allowed_modifications": ["package-lock.json — derived"],
            "tasks": [
                {
                    "id": "t1",
                    "goal": "update dep",
                    "allowed_modifications": ["yarn.lock — derived"],
                },
            ],
        }
        text = f"```json\n{json.dumps(plan_json)}\n```"
        plan = parse_plan(text)

        # Plan-level and per-task both survive the real JSON parse path.
        assert plan.allowed_modifications == ("package-lock.json — derived",)
        assert plan.subtasks[0].allowed_modifications == ("yarn.lock — derived",)
        assert plan.has_contract is True

    def test_contract_absent_plan_has_empty_fields(self) -> None:
        plan_json = {
            "short_summary": "No contract.",
            "planning_context": "no contract",
            "tasks": [{"id": "t1", "goal": "x"}],
        }
        text = f"```json\n{json.dumps(plan_json)}\n```"
        plan = parse_plan(text)

        assert plan.goal is None
        assert plan.acceptance_criteria == ()
        assert plan.owned_files == ()
        assert plan.allowed_modifications == ()
        assert plan.subtasks[0].allowed_modifications == ()
        assert plan.has_contract is False

    def test_malformed_contract_fails_before_dag_validation(self) -> None:
        """A bad-typed contract field fails plan parse cleanly — the run
 cannot reach BUILD with a malformed structured plan."""
        plan_json = {
            "short_summary": "x",
            "planning_context": "x",
            "tasks": [{"id": "t1", "goal": "y"}],
            "acceptance_criteria": "not a list",
        }
        text = f"```json\n{json.dumps(plan_json)}\n```"
        with pytest.raises(PlanSchemaError, match="acceptance_criteria"):
            parse_plan(text)


# ─────────────────────────────────────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────────────────────────────────────


def _make_plan(**overrides: object) -> ParsedPlan:
    base = {
        "short_summary": "x",
        "planning_context": "x",
        "subtasks": (),
        "source": "json",
    }
    return ParsedPlan(**(base | overrides))  # type: ignore[arg-type]


class TestRenderPlanContract:
    def test_empty_plan_renders_empty_string(self) -> None:
        assert render_plan_contract(_make_plan()) == ""

    def test_none_plan_renders_empty_string(self) -> None:
        assert render_plan_contract(None) == ""

    def test_legacy_plan_without_has_contract_attr_renders_empty(self) -> None:
        """Defensive guard: renderer accepts objects with no contract fields
 without exploding."""

        class _Legacy:
            pass

        assert render_plan_contract(_Legacy()) == ""  # type: ignore[arg-type]

    def test_full_contract_block_shape(self) -> None:
        plan = _make_plan(
            goal="Fix bug",
            acceptance_criteria=("AC1", "AC2"),
            owned_files=("a.py", "b.py"),
            commands_to_run=("pytest -q",),
            risks=("don't break X",),
            review_focus=("coverage",),
            mcp_context=({"server": "github", "tool": "get_issue", "args": {"n": 1}},),
        )
        out = render_plan_contract(plan)

        assert out.startswith("## Plan Contract")
        assert "**Goal:** Fix bug" in out
        assert "**Acceptance criteria:**" in out
        assert "- AC1" in out
        assert "- AC2" in out
        assert "**Owned files:**" in out
        assert "- a.py" in out
        assert "**Commands to run:**" in out
        assert "- pytest -q" in out
        assert "**Risks:**" in out
        assert "**Review focus:**" in out
        assert "**MCP context:**" in out
        assert "`github.get_issue`" in out
        assert out.endswith("\n")

    def test_partial_contract_omits_empty_sections(self) -> None:
        plan = _make_plan(goal="just a goal")
        out = render_plan_contract(plan)

        assert "**Goal:** just a goal" in out
        assert "**Acceptance criteria:**" not in out
        assert "**Owned files:**" not in out
        assert "**Risks:**" not in out
        # T4: companion-modifications section absent when neither level set.
        assert "**Allowed companion modifications:**" not in out


class TestRenderAllowedModificationsSection:
    """T4: render_plan_contract aggregates plan-level and per-task
    ``allowed_modifications`` into one section after ``Owned files``,
    tagging per-task entries with ``[<task-id>]``."""

    def test_plan_level_entries_rendered_verbatim(self) -> None:
        plan = _make_plan(
            goal="g",
            allowed_modifications=("package-lock.json — derived",),
        )
        out = render_plan_contract(plan)

        assert "**Allowed companion modifications:**" in out
        assert "- package-lock.json — derived" in out

    def test_per_task_entries_tagged_with_task_id(self) -> None:
        from agents.entities import SubTask

        plan = _make_plan(
            subtasks=(
                SubTask(
                    id="t7",
                    goal="bump deps",
                    allowed_modifications=("yarn.lock — derived",),
                ),
            ),
        )
        out = render_plan_contract(plan)

        # Per-task-only plan still renders (has_contract guard accounts
        # for per-task allowed_modifications).
        assert "## Plan Contract" in out
        assert "**Allowed companion modifications:**" in out
        assert "- [t7] yarn.lock — derived" in out

    def test_both_levels_aggregate_in_one_section(self) -> None:
        from agents.entities import SubTask

        plan = _make_plan(
            goal="g",
            allowed_modifications=("top.lock — derived",),
            subtasks=(
                SubTask(
                    id="t1",
                    goal="x",
                    allowed_modifications=("task.snap — regenerated",),
                ),
            ),
        )
        out = render_plan_contract(plan)

        assert out.count("**Allowed companion modifications:**") == 1
        assert "- top.lock — derived" in out
        assert "- [t1] task.snap — regenerated" in out

    def test_section_absent_when_both_levels_empty(self) -> None:
        from agents.entities import SubTask

        plan = _make_plan(
            goal="g",
            owned_files=("a.py",),
            subtasks=(SubTask(id="t1", goal="x"),),
        )
        out = render_plan_contract(plan)

        assert "## Plan Contract" in out
        assert "**Allowed companion modifications:**" not in out


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end propagation: subtask prompt picks up plan_contract
# ─────────────────────────────────────────────────────────────────────────────


class TestSubtaskPromptCarriesContract:
    def test_contract_block_lands_in_subtask_prompt(self) -> None:
        from agents.entities import SubTask
        from pipeline.plugins import PluginConfig
        from pipeline.prompts.subtask import build_subtask_prompt

        plan = _make_plan(
            goal="Reject invalid payloads",
            acceptance_criteria=("Invalid → 400",),
        )
        contract = render_plan_contract(plan)
        sub = SubTask(id="t1", goal="apply fix")

        turn, _ = build_subtask_prompt(
            sub, PluginConfig(), plan_contract=contract,
        )
        prompt = turn.text  # P1: build_subtask_prompt returns a PromptTurn

        assert "## Plan Contract" in prompt
        assert "Reject invalid payloads" in prompt
        assert "Invalid → 400" in prompt
        # Contract lands ahead of the current executable subtask block.
        assert prompt.index("## Plan Contract") < prompt.index(
            "## Current Executable Subtask")

    def test_subtask_prompt_without_contract_is_unchanged(self) -> None:
        from agents.entities import SubTask
        from pipeline.plugins import PluginConfig
        from pipeline.prompts.subtask import build_subtask_prompt

        sub = SubTask(id="t1", goal="x")
        turn, _ = build_subtask_prompt(sub, PluginConfig())
        # No plan-contract section/part when none is supplied. (The execution
        # rules text references "Plan Contract" generically, so assert on the
        # rendered section header + the part id rather than the bare phrase.)
        assert "## Plan Contract" not in turn.text
        assert not any(p.id == "plan_contract:typed_plan" for p in turn.parts)
