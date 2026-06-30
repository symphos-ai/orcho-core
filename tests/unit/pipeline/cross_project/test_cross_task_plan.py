"""Focus tests for the typed cross-task-plan adapter (``task_plan.py``).

These pin that ``normalize_cross_task_plan`` is a faithful typed *view* over an
already-validated ``CrossPlanParse``: units come out in supplied-alias order
(not JSON order), ``unit_id == alias``, the dropped-by-parsed-view fields
(``goal`` / ``files`` / ``produces`` / ``consumes``) are read from
``parse.data``, and the result is equivalent to the current parsed seam
(``"\\n".join(implementation_order)`` and ``{alias: spec}``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipeline.cross_project.plan_parser import parse_cross_plan
from pipeline.cross_project.task_plan import (
    CrossTaskPlan,
    CrossTaskUnit,
    normalize_cross_task_plan,
)

_ALIASES = ["api", "web"]


def _multi_alias_plan_json() -> str:
    """One valid multi-alias plan, subtasks emitted in REVERSED order.

    JSON lists ``web`` before ``api`` while the supplied alias order is
    ``[api, web]`` — so any assert on supplied-order routing is meaningful.
    """
    return json.dumps(
        {
            "short_summary": "share the email field across api + web",
            "interface_contract": "POST /api/users {name, email}",
            "implementation_order": ["land api schema", "wire web form"],
            "subtasks": [
                {
                    "alias": "web",
                    "goal": "render the email input",
                    "spec": "web implement spec",
                    "depends_on": ["api"],
                    "files": ["[web]/src/form.tsx"],
                    "produces": "submitted email payload",
                    "consumes": "api user contract",
                },
                {
                    "alias": "api",
                    "goal": "accept the email field",
                    "spec": "api implement spec",
                    "depends_on": [],
                    "files": ["[api]/users.py"],
                    "produces": "persisted user row",
                    "consumes": "web form submission",
                },
            ],
        }
    )


def _single_alias_plan_json() -> str:
    """A valid single-alias plan with an empty interface_contract."""
    return json.dumps(
        {
            "short_summary": "tidy the api only",
            "interface_contract": "",
            "implementation_order": ["land api change"],
            "subtasks": [
                {
                    "alias": "api",
                    "goal": "rename the field",
                    "spec": "api implement spec",
                    "depends_on": [],
                    "files": ["[api]/users.py"],
                    "produces": "renamed column",
                    "consumes": "",
                },
            ],
        }
    )


def test_units_ordered_by_supplied_aliases_not_json_order() -> None:
    parse = parse_cross_plan(_multi_alias_plan_json(), _ALIASES)
    plan = normalize_cross_task_plan(parse, _ALIASES)

    assert isinstance(plan, CrossTaskPlan)
    assert [u.alias for u in plan.units] == _ALIASES
    for unit in plan.units:
        assert isinstance(unit, CrossTaskUnit)
        assert unit.unit_id == unit.alias


def test_unit_fields_mapped_from_normalized_data() -> None:
    parse = parse_cross_plan(_multi_alias_plan_json(), _ALIASES)
    plan = normalize_cross_task_plan(parse, _ALIASES)
    by_alias = plan.units_by_alias()

    api = by_alias["api"]
    assert api.goal == "accept the email field"
    assert api.spec == "api implement spec"
    assert api.depends_on == ()
    assert api.files == ("[api]/users.py",)
    assert api.produces == "persisted user row"
    assert api.consumes == "web form submission"

    web = by_alias["web"]
    assert web.goal == "render the email input"
    assert web.spec == "web implement spec"
    assert web.depends_on == ("api",)
    assert web.files == ("[web]/src/form.tsx",)
    assert web.produces == "submitted email payload"
    assert web.consumes == "api user contract"


def test_plan_level_fields() -> None:
    parse = parse_cross_plan(_multi_alias_plan_json(), _ALIASES)
    plan = normalize_cross_task_plan(parse, _ALIASES)

    assert plan.short_summary == "share the email field across api + web"
    assert plan.interface_contract == "POST /api/users {name, email}"
    assert plan.implementation_order == ("land api schema", "wire web form")


def test_equivalence_with_current_parsed_seam() -> None:
    parse = parse_cross_plan(_multi_alias_plan_json(), _ALIASES)
    plan = normalize_cross_task_plan(parse, _ALIASES)

    # implementation_order joins back to the single \n-joined string the
    # current parsed view threads into the child prompt.
    assert "\n".join(plan.implementation_order) == parse.parsed.implementation_order

    # {alias: spec} routing map matches the current parsed routing map.
    assert {u.alias: u.spec for u in plan.units} == parse.parsed.subtasks_dict()


def test_run_project_dispatch_consumes_typed_task_plan(tmp_path, monkeypatch) -> None:
    """``run_project_dispatch`` routes the per-alias spec from the typed plan
    into each child request and threads the plan's interface_contract /
    implementation_order into the written handoff."""
    from pipeline.cross_project import project_dispatch

    api_dir = tmp_path / "api"
    web_dir = tmp_path / "web"
    api_dir.mkdir()
    web_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    plan = normalize_cross_task_plan(
        parse_cross_plan(_multi_alias_plan_json(), _ALIASES), _ALIASES,
    )

    captured: list = []

    def _stub_run_project_pipeline(request):
        captured.append(request)
        return SimpleNamespace(session={"status": "done", "phases": {}})

    monkeypatch.setattr(
        project_dispatch, "run_project_pipeline", _stub_run_project_pipeline,
    )

    ctx = project_dispatch.ProjectDispatchContext(
        task="full cross task",
        projects={"api": api_dir, "web": web_dir},
        task_plan=plan,
        resume_from=None,
        dry_run=False,
        max_rounds=2,
        code_model="stub",
        phase_config=None,
        child_profile=object(),  # truthy → loop runs
        requested_profile_name="advanced",
        has_global_plan=True,
        provider=MagicMock(),
        hypothesis_enabled=False,
        followup_session_seeds_per_alias=None,
        run_dir=run_dir,
        output_dir=False,
        plan_output="fallback plan markdown",
        plan_review_dict=None,
        cross_ckpt={"sub_status": {}},
        session={"phases": {"projects": {}}},
        cross_phase_usage={},
        ports=MagicMock(),
        terminal=False,
    )

    result = project_dispatch.run_project_dispatch(ctx)
    assert result.paused is False

    # Each child request carried its own per-alias spec from the typed plan.
    tasks_by_alias = {req.project_alias: req.task for req in captured}
    assert tasks_by_alias == {
        "api": "api implement spec",
        "web": "web implement spec",
    }

    # The persisted handoff carries the plan's shared slices.
    handoff = json.loads(
        (run_dir / "api" / "implementation_handoff.json").read_text(
            encoding="utf-8",
        )
    )
    assert handoff["interface_contract"] == "POST /api/users {name, email}"
    assert handoff["implementation_order"] == "land api schema\nwire web form"
    assert handoff["project_subtask"] == "api implement spec"


def test_single_alias_plan_with_empty_interface_contract() -> None:
    aliases = ["api"]
    parse = parse_cross_plan(_single_alias_plan_json(), aliases)
    plan = normalize_cross_task_plan(parse, aliases)

    assert plan.interface_contract == ""
    assert plan.implementation_order == ("land api change",)
    assert [u.alias for u in plan.units] == aliases
    only = plan.units[0]
    assert only.unit_id == "api"
    assert only.goal == "rename the field"
    assert only.consumes == ""
    assert "\n".join(plan.implementation_order) == parse.parsed.implementation_order
    assert {u.alias: u.spec for u in plan.units} == parse.parsed.subtasks_dict()
