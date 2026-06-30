"""Tests for :mod:`pipeline.cross_project.plan_parser` (ADR 0054).

The cross architect emits one JSON object validated against
``core.contracts.cross_plan_schema``. ``parse_cross_plan`` returns a
:class:`CrossPlanParse` bundling the normalized data, a structured
:class:`ParsedCrossPlan` (routing + CLI render), and parse warnings.
``render_cross_plan_markdown`` derives the human-readable cross_plan.md.
A malformed/invalid object raises ``CrossPlanParseError`` (never ``None``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cross_project.plan_parser import (
    CrossPlanParse,
    CrossPlanParseError,
    ParsedCrossPlan,
    aliasize_cross_plan,
    cross_plan_document,
    parse_cross_plan,
    render_cross_plan_markdown,
    write_cross_plan_artifacts,
)


def _plan(
    aliases: tuple[str, ...] = ("api", "web"),
    *,
    interface_contract: str = "POST /api/users {name, email}",
    order: list[str] | None = None,
    deps: dict[str, list[str]] | None = None,
) -> dict:
    deps = deps if deps is not None else {"web": ["api"]}
    return {
        "short_summary": "coordinate the email field across api + web",
        "interface_contract": interface_contract,
        "implementation_order": order or ["api first", "web second"],
        "subtasks": [
            {
                "alias": a,
                "goal": f"{a} goal",
                "spec": f"{a} detailed spec",
                "depends_on": deps.get(a, []),
                "files": [f"[{a}]/src/file"],
                "produces": f"{a} output",
                "consumes": f"{a} input",
            }
            for a in aliases
        ],
    }


# ── happy path ────────────────────────────────────────────────────────


def test_parse_valid_returns_structured_view() -> None:
    result = parse_cross_plan(json.dumps(_plan()), ["api", "web"])
    assert isinstance(result, CrossPlanParse)
    assert isinstance(result.parsed, ParsedCrossPlan)
    assert result.parse_warnings == ()
    assert result.parsed.subtasks_dict() == {
        "api": "api detailed spec",
        "web": "web detailed spec",
    }
    # implementation_order list joined to a single string (the only join site).
    assert result.parsed.implementation_order == "api first\nweb second"
    # typed dependency edges preserved.
    assert dict(result.parsed.dependencies) == {"api": (), "web": ("api",)}


def test_to_render_dict_shape_is_stable() -> None:
    parsed = parse_cross_plan(json.dumps(_plan()), ["api", "web"]).parsed
    rd = parsed.to_render_dict()
    assert set(rd) == {
        "interface_contract", "implementation_order", "subtasks",
        "aliases_missing",
    }
    assert rd["aliases_missing"] == []  # coverage guaranteed by the schema


def test_prose_wrapped_object_recovers_with_warning() -> None:
    text = "Here is the plan:\n" + json.dumps(_plan()) + "\nThanks!"
    result = parse_cross_plan(text, ["api", "web"])
    assert result.parsed.subtasks_dict()["api"] == "api detailed spec"
    assert result.parse_warnings  # non-fatal recovery recorded


# ── render ──────────────────────────────────────────────────────────────


def test_render_markdown_canonical_headings() -> None:
    data = parse_cross_plan(json.dumps(_plan()), ["api", "web"]).data
    md = render_cross_plan_markdown(data)
    assert "## Interface Contract" in md
    assert "## Per-Project Subtasks" in md
    assert "### [api]" in md
    assert "### [web]" in md
    assert "## Implementation Order" in md
    assert "Depends on: api" in md  # web's edge surfaced
    assert "=== SUBTASK" not in md  # marker grammar is gone


def test_render_is_deterministic() -> None:
    data = parse_cross_plan(json.dumps(_plan()), ["api", "web"]).data
    assert render_cross_plan_markdown(data) == render_cross_plan_markdown(data)


def test_render_orders_subtasks_by_supplied_aliases() -> None:
    """When aliases are supplied, the render orders subtasks in supplied
    (routing) order regardless of the JSON-array order — so the audit render
    matches dispatch ordering."""
    # JSON lists web first, but supplied order is [api, web].
    data = parse_cross_plan(
        json.dumps(_plan(aliases=("web", "api"))), ["api", "web"],
    ).data
    md = render_cross_plan_markdown(data, ["api", "web"])
    assert md.index("### [api]") < md.index("### [web]")
    # Without aliases the render falls back to JSON order (web first here).
    md_json_order = render_cross_plan_markdown(data)
    assert md_json_order.index("### [web]") < md_json_order.index("### [api]")


def test_aliasize_cross_plan_makes_canonical_data_leak_clean() -> None:
    """``aliasize_cross_plan`` rewrites absolute project roots to ``[alias]/``
    form across every string field — so the persisted ``cross_plan.json`` (the
    canonical object) never carries an absolute filesystem path, not just the
    rendered markdown."""
    projects = {"api": Path("/abs/ws/api"), "web": Path("/abs/ws/web")}
    leaky = _plan()
    web = next(s for s in leaky["subtasks"] if s["alias"] == "web")
    web["files"] = ["/abs/ws/web/src/x.ts"]
    web["spec"] = "edit /abs/ws/web/src/x.ts to match the contract"
    leaky["interface_contract"] = "shared at /abs/ws/api/server.py"

    parse = parse_cross_plan(json.dumps(leaky), ["api", "web"])
    norm = aliasize_cross_plan(parse, projects, ["api", "web"])

    assert "/abs/ws" not in json.dumps(norm.data), (
        "absolute path leaked into the canonical cross_plan.json object"
    )
    web_norm = next(s for s in norm.data["subtasks"] if s["alias"] == "web")
    assert web_norm["files"] == ["[web]/src/x.ts"]
    assert "[web]/src/x.ts" in web_norm["spec"]
    assert "[api]/server.py" in norm.data["interface_contract"]
    # Idempotent: re-aliasizing already-clean data is a no-op.
    again = aliasize_cross_plan(norm, projects, ["api", "web"])
    assert again.data == norm.data


def test_write_cross_plan_artifacts_md_matches_document(tmp_path: Path) -> None:
    """``cross_plan.md`` on disk is byte-identical to the returned document
    (the same text fed to the reviewer artifact and the dispatch handoff), and
    ``cross_plan.json`` is the leak-clean normalized object."""
    projects = {"api": Path("/abs/ws/api"), "web": Path("/abs/ws/web")}
    parse = parse_cross_plan(json.dumps(_plan()), ["api", "web"])
    norm, document = write_cross_plan_artifacts(
        tmp_path, parse, task="Align email", projects=projects,
        aliases=["api", "web"],
    )
    md_on_disk = (tmp_path / "cross_plan.md").read_text(encoding="utf-8")
    assert md_on_disk == document
    assert document == cross_plan_document(
        norm.data, task="Align email", aliases=["api", "web"],
    )
    assert document.startswith("# Cross-Project Plan\n\nTask: Align email")
    persisted = json.loads((tmp_path / "cross_plan.json").read_text("utf-8"))
    assert persisted == norm.data


# ── rejects (never None — fail at the boundary) ──────────────────────────


def test_unparseable_raises() -> None:
    with pytest.raises(CrossPlanParseError):
        parse_cross_plan("not json at all", ["api", "web"])


def test_missing_alias_coverage_raises() -> None:
    plan = _plan(("api",))  # web missing
    with pytest.raises(CrossPlanParseError, match="no subtask for supplied"):
        parse_cross_plan(json.dumps(plan), ["api", "web"])


def test_extra_alias_raises() -> None:
    plan = _plan(("api", "web", "stats"))
    with pytest.raises(CrossPlanParseError, match="not in the supplied aliases"):
        parse_cross_plan(json.dumps(plan), ["api", "web"])


def test_cycle_raises() -> None:
    plan = _plan(deps={"api": ["web"], "web": ["api"]})
    with pytest.raises(CrossPlanParseError, match="dependency cycle"):
        parse_cross_plan(json.dumps(plan), ["api", "web"])


def test_self_edge_raises() -> None:
    plan = _plan(deps={"web": ["web"]})
    with pytest.raises(CrossPlanParseError, match="depends on itself"):
        parse_cross_plan(json.dumps(plan), ["api", "web"])


def test_unknown_dependency_raises() -> None:
    plan = _plan(deps={"web": ["ghost"]})
    with pytest.raises(CrossPlanParseError, match="unknown alias"):
        parse_cross_plan(json.dumps(plan), ["api", "web"])


def test_multi_alias_requires_interface_contract() -> None:
    plan = _plan(interface_contract="")
    with pytest.raises(
        CrossPlanParseError, match="interface_contract must be non-empty",
    ):
        parse_cross_plan(json.dumps(plan), ["api", "web"])


def test_prose_wrapped_invalid_surfaces_schema_error() -> None:
    # A candidate-shaped object that fails validation, wrapped in prose,
    # surfaces the schema error (not the generic "no JSON" message) so the
    # synthetic replan critique is actionable.
    plan = _plan(("api",))  # coverage gap
    text = "plan:\n" + json.dumps(plan) + "\ndone"
    with pytest.raises(CrossPlanParseError, match="no subtask for supplied"):
        parse_cross_plan(text, ["api", "web"])


# ── single-alias edge ─────────────────────────────────────────────────


def test_single_alias_allows_empty_interface_contract() -> None:
    plan = _plan(("api",), interface_contract="", deps={})
    result = parse_cross_plan(json.dumps(plan), ["api"])
    assert result.parsed.subtasks_dict() == {"api": "api detailed spec"}
    assert result.parsed.interface_contract == ""
