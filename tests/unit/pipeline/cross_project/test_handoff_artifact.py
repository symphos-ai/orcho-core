"""ADR 0050 — structured cross handoff (JSON source of truth).

The cross orchestrator writes ``implementation_handoff.json`` (canonical)
+ ``implementation_handoff.md`` (derived audit view) and validates the
typed handoff on write. The child loads + validates the JSON and renders
the runtime body from the typed object, so a stray field can no longer
become a misleading prompt instruction.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cross_project.handoff import (
    Handoff,
    load_handoff,
    render_handoff_markdown,
    validate_handoff,
    write_handoff,
)


def _handoff(**overrides) -> Handoff:
    base = dict(
        parent_run_id="20260528_000000",
        profile="advanced",
        alias="api",
        project_path="/abs/source/api",
        approved_cross_plan_path="/run/cross_plan.md",
        full_cross_plan_path="/run/cross_plan.md",
        full_cross_plan_markdown="# Cross-Project Plan\n\nDetails.\n",
        cross_validation_summary="Looks good.",
        cross_validation_verdict={"verdict": "APPROVED"},
        project_subtask="Wire the endpoint",
        sibling_aliases=("web",),
    )
    base.update(overrides)
    return Handoff(**base)


# ── write: JSON canonical, md derived ─────────────────────────────────


def test_write_handoff_returns_json_path(tmp_path: Path) -> None:
    alias_dir = tmp_path / "api"
    alias_dir.mkdir()
    out = write_handoff(_handoff(), alias_dir)
    assert out == alias_dir / "implementation_handoff.json"
    assert out.is_file()
    # The audit markdown sidecar is still written.
    assert (alias_dir / "implementation_handoff.md").is_file()


def test_write_handoff_json_is_round_trippable(tmp_path: Path) -> None:
    alias_dir = tmp_path / "api"
    alias_dir.mkdir()
    h = _handoff()
    json_path = write_handoff(h, alias_dir)
    loaded = load_handoff(json_path)
    assert loaded == h


def test_markdown_is_rendered_from_typed_object(tmp_path: Path) -> None:
    alias_dir = tmp_path / "api"
    alias_dir.mkdir()
    json_path = write_handoff(_handoff(project_subtask="UNIQUE_SUBTASK"), alias_dir)
    md = (alias_dir / "implementation_handoff.md").read_text(encoding="utf-8")
    rendered = render_handoff_markdown(load_handoff(json_path))
    assert md == rendered
    assert "UNIQUE_SUBTASK" in md


# ── ADR 0052: structured plan slices vs full-plan fallback ────────────


def test_render_uses_structured_slices_when_present() -> None:
    body = render_handoff_markdown(
        _handoff(
            interface_contract="POST /api/users → {name, email}",
            implementation_order="1. api verify  2. web rename",
            full_cross_plan_markdown="SHOULD_NOT_APPEAR_full_plan_dump",
        )
    )
    assert "## Interface contract" in body
    assert "POST /api/users → {name, email}" in body
    assert "## Implementation order" in body
    assert "1. api verify  2. web rename" in body
    # The full-plan dump (and its sibling-subtask noise) is dropped.
    assert "## Full cross plan" not in body
    assert "SHOULD_NOT_APPEAR_full_plan_dump" not in body


def test_render_falls_back_to_full_plan_without_slices() -> None:
    body = render_handoff_markdown(
        _handoff(full_cross_plan_markdown="# Whole plan\n\nverbatim.\n")
    )
    assert "## Full cross plan" in body
    assert "verbatim." in body
    assert "## Interface contract" not in body


def test_render_uses_slices_when_only_one_present() -> None:
    # A single non-empty slice is enough to take the structured path.
    body = render_handoff_markdown(
        _handoff(
            interface_contract="just the contract",
            full_cross_plan_markdown="FALLBACK_PLAN",
        )
    )
    assert "## Interface contract" in body
    assert "FALLBACK_PLAN" not in body
    assert "## Implementation order" in body  # rendered with "(none)"


def test_round_trips_structured_slices(tmp_path: Path) -> None:
    alias_dir = tmp_path / "api"
    alias_dir.mkdir()
    h = _handoff(interface_contract="IC", implementation_order="IO")
    loaded = load_handoff(write_handoff(h, alias_dir))
    assert loaded == h
    assert loaded.interface_contract == "IC"
    assert loaded.implementation_order == "IO"


def test_load_defaults_missing_structured_slices(tmp_path: Path) -> None:
    # A handoff JSON written before ADR 0052 has neither slice; load must
    # default them to "" (and render via the full-plan fallback).
    p = tmp_path / "implementation_handoff.json"
    payload = {
        "parent_run_id": "r", "profile": "advanced", "alias": "api",
        "project_path": "/s", "approved_cross_plan_path": "/c",
        "full_cross_plan_path": "/c", "full_cross_plan_markdown": "x",
        "cross_validation_summary": "ok", "cross_validation_verdict": {},
        "project_subtask": "do", "sibling_aliases": [],
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_handoff(p)
    assert loaded.interface_contract == ""
    assert loaded.implementation_order == ""


# ── leak guard: source project_path never in the runtime body ─────────


def test_rendered_body_never_contains_source_project_path() -> None:
    h = _handoff(project_path="/abs/source/SHOULD_NOT_LEAK")
    body = render_handoff_markdown(h)
    assert "/abs/source/SHOULD_NOT_LEAK" not in body
    assert "SHOULD_NOT_LEAK" not in body


def test_validate_rejects_source_path_leak_in_body() -> None:
    # A plan markdown that echoes the source path would leak it into the
    # rendered runtime body — validate must fail at the cross level.
    leaky = _handoff(
        project_path="/abs/source/api",
        full_cross_plan_markdown="Work in /abs/source/api now.",
    )
    with pytest.raises(ValueError, match="leaks the source project_path"):
        validate_handoff(leaky)


# ── validation: missing/contradictory fields fail on write ────────────


@pytest.mark.parametrize(
    "field",
    ["parent_run_id", "profile", "alias", "project_subtask",
     "full_cross_plan_markdown"],
)
def test_write_rejects_missing_required_field(tmp_path: Path, field: str) -> None:
    alias_dir = tmp_path / "api"
    alias_dir.mkdir()
    h = _handoff(**{field: "   "})
    with pytest.raises(ValueError, match="missing required field"):
        write_handoff(h, alias_dir)
    # Nothing partially written.
    assert not (alias_dir / "implementation_handoff.json").exists()


# ── load: malformed / wrong-shape JSON ────────────────────────────────


def test_load_rejects_non_object_json(tmp_path: Path) -> None:
    p = tmp_path / "implementation_handoff.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        load_handoff(p)


def test_load_rejects_unparseable_json(tmp_path: Path) -> None:
    p = tmp_path / "implementation_handoff.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="parseable"):
        load_handoff(p)


def test_load_rejects_unknown_field(tmp_path: Path) -> None:
    p = tmp_path / "implementation_handoff.json"
    payload = {
        "parent_run_id": "r", "profile": "advanced", "alias": "api",
        "project_path": "/s", "approved_cross_plan_path": "/c",
        "full_cross_plan_path": "/c", "full_cross_plan_markdown": "x",
        "cross_validation_summary": "ok", "cross_validation_verdict": {},
        "project_subtask": "do", "sibling_aliases": [],
        "surprise_field": "boom",
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown field"):
        load_handoff(p)


def test_load_rejects_missing_field(tmp_path: Path) -> None:
    p = tmp_path / "implementation_handoff.json"
    payload = {"parent_run_id": "r", "profile": "advanced", "alias": "api"}
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing field"):
        load_handoff(p)


def test_load_defaults_missing_sibling_aliases(tmp_path: Path) -> None:
    p = tmp_path / "implementation_handoff.json"
    payload = {
        "parent_run_id": "r", "profile": "advanced", "alias": "api",
        "project_path": "/s", "approved_cross_plan_path": "/c",
        "full_cross_plan_path": "/c", "full_cross_plan_markdown": "x",
        "cross_validation_summary": "ok", "cross_validation_verdict": {},
        "project_subtask": "do",
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_handoff(p)
    assert loaded.sibling_aliases == ()
