"""Guard for the cross plan -> task-execution seam (preparation stage).

This is NOT a re-test of the parser (render / reject / aliasize / write
artifacts already live in ``test_cross_plan_parser.py``). It pins the exact
slice of ``parse_cross_plan`` output that ``pipeline.cross_project`` app +
``project_dispatch`` consume today, so the future typed adapter
(``pipeline/cross_project/task_plan.py`` :: ``normalize_cross_task_plan``,
producing ``CrossTaskPlan`` / ``CrossTaskUnit`` — see
the cross-task-plan-model planning record (internal)) is forced to preserve it.

The seam has two halves:

* ``CrossPlanParse.parsed`` — the routing/render subset dispatch reads now
  (``subtasks_dict`` in supplied-alias order, ``interface_contract``,
  the ``\\n``-joined ``implementation_order``, typed cross-alias
  ``dependencies`` edges).
* ``CrossPlanParse.data`` — the normalized validated dict that still carries
  ``goal`` / ``files`` / ``produces`` / ``consumes`` per subtask (the parsed
  view drops them; the adapter must read them from ``data``).

This module intentionally does NOT import ``pipeline.cross_project.task_plan``:
that module does not exist yet and importing it would break collection.
"""

from __future__ import annotations

import json

from pipeline.cross_project.plan_parser import parse_cross_plan

_ALIASES = ["api", "web"]


def _multi_alias_plan_json() -> str:
    """One valid multi-alias plan, with subtasks emitted in REVERSED order.

    JSON lists ``web`` before ``api`` while the supplied alias order is
    ``[api, web]`` — so any assert on supplied-order routing is meaningful
    (it cannot accidentally pass on JSON-array order).
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


def test_parsed_routing_seam_matches_dispatch_consumption() -> None:
    parsed = parse_cross_plan(_multi_alias_plan_json(), _ALIASES).parsed

    # subtasks_dict is the {alias: spec} routing map dispatch loops over, keyed
    # in supplied-alias order even though the JSON lists web first.
    routing = parsed.subtasks_dict()
    assert list(routing) == _ALIASES
    assert routing == {"api": "api implement spec", "web": "web implement spec"}

    # interface_contract is the shared surface dispatch hoists out of the loop.
    assert parsed.interface_contract == "POST /api/users {name, email}"

    # implementation_order is the \n-joined string dispatch threads into the
    # child prompt context (the single join site).
    assert parsed.implementation_order == "land api schema\nwire web form"

    # dependencies are the typed cross-alias edges (ADR 0057 ordering input).
    assert dict(parsed.dependencies) == {"api": (), "web": ("api",)}


def test_normalized_data_retains_fields_dropped_by_parsed_view() -> None:
    # The adapter must read goal/files/produces/consumes from CrossPlanParse.data
    # because ParsedCrossPlan drops them — pin that they survive normalization.
    result = parse_cross_plan(_multi_alias_plan_json(), _ALIASES)
    by_alias = {st["alias"]: st for st in result.data["subtasks"]}

    assert set(by_alias) == set(_ALIASES)
    for alias in _ALIASES:
        st = by_alias[alias]
        for key in ("goal", "files", "produces", "consumes"):
            assert key in st, f"{alias}.{key} dropped from normalized data"

    assert by_alias["api"]["goal"] == "accept the email field"
    assert by_alias["api"]["files"] == ["[api]/users.py"]
    assert by_alias["api"]["produces"] == "persisted user row"
    assert by_alias["api"]["consumes"] == "web form submission"
    assert by_alias["web"]["goal"] == "render the email input"
    assert by_alias["web"]["files"] == ["[web]/src/form.tsx"]
    assert by_alias["web"]["produces"] == "submitted email payload"
    assert by_alias["web"]["consumes"] == "api user contract"
