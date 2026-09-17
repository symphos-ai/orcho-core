"""Cross-plan fallback coverage for the evidence plan record."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cross_project.plan_parser import (
    parse_cross_plan,
    write_cross_plan_artifacts,
)
from pipeline.evidence import collect_evidence
from pipeline.evidence.schema import validate_bundle

ALIASES = ["api", "web"]


def _cross_plan(*, aliases: list[str] = ALIASES) -> dict:
    return {
        "short_summary": "Coordinate API and web changes.",
        "interface_contract": "API returns a stable payload.",
        "implementation_order": ["Update API, then consume it in web."],
        "subtasks": [
            {
                "alias": alias,
                "goal": f"Update {alias}.",
                "spec": f"Implement the {alias} portion.",
            }
            for alias in aliases
        ],
    }


def _write_run(run_dir: Path, *, events: list[dict] | None = None) -> None:
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "cross-plan-test",
        "status": "done",
        "projects": {alias: {"path": f"/{alias}"} for alias in ALIASES},
    }), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events or []) + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text("{}", encoding="utf-8")


def _write_canonical_cross_plan(run_dir: Path) -> None:
    parsed = parse_cross_plan(json.dumps(_cross_plan()), ALIASES)
    write_cross_plan_artifacts(
        run_dir,
        parsed,
        task="Coordinate API and web changes.",
        projects={alias: Path(f"/{alias}") for alias in ALIASES},
        aliases=ALIASES,
    )


def _plan_parsed_event(payload: dict) -> dict:
    return {
        "seq": 1,
        "ts": "2026-07-28T14:00:00Z",
        "kind": "plan.parsed",
        "phase": "plan",
        "payload": payload,
    }


def test_cross_plan_record_projects_valid_canonical_artifact(tmp_path: Path) -> None:
    _write_run(tmp_path)
    _write_canonical_cross_plan(tmp_path)

    plan = collect_evidence(tmp_path)["plan"]

    assert plan == {
        "source": "json",
        "short_summary": "Coordinate API and web changes.",
        "planning_context": "API returns a stable payload.",
        "subtask_count": 2,
        "has_contract": True,
        "goal": None,
        "acceptance_criteria": [],
        "owned_files": [],
        "commands_to_run": [],
        "risks": [],
        "review_focus": [],
        "mcp_context": [],
        "subtasks": [],
    }


def test_a_cross_plan_record_never_ships_with_a_criterion_matrix(
    tmp_path: Path,
) -> None:
    """The cross projection reports ``source="json"`` with no criteria.

    ADR 0188's plan cross-check treats a projected source as authoritative, so
    this combination would demand an empty matrix. It is unreachable by
    construction and this pins it: the cross branch is chosen only when there
    is no mono ``plan.parsed`` event, and the matrix is built only from a
    ``parsed_plan.json`` that a cross parent never writes.
    """
    _write_run(tmp_path)
    _write_canonical_cross_plan(tmp_path)

    bundle = collect_evidence(tmp_path)

    assert bundle["plan"]["source"] == "json"
    assert bundle["plan"]["acceptance_criteria"] == []
    assert "criterion_matrix" not in bundle
    validate_bundle(bundle)


def test_cross_plan_record_uses_run_start_aliases_for_single_project(
    tmp_path: Path,
) -> None:
    alias = "api"
    run_start = {
        "seq": 1,
        "ts": "2026-07-28T14:00:00Z",
        "kind": "run.start",
        "phase": None,
        "payload": {"projects": {alias: {"path": "/api"}}},
    }
    _write_run(tmp_path, events=[run_start])
    (tmp_path / "meta.json").write_text(json.dumps({
        "run_id": "cross-plan-test", "status": "done",
    }), encoding="utf-8")
    data = _cross_plan(aliases=[alias])
    data["interface_contract"] = ""
    parsed = parse_cross_plan(json.dumps(data), [alias])
    write_cross_plan_artifacts(
        tmp_path,
        parsed,
        task="Single-project cross continuation.",
        projects={alias: Path("/api")},
        aliases=[alias],
    )

    plan = collect_evidence(tmp_path)["plan"]

    assert plan["source"] == "json"
    assert plan["planning_context"] == ""
    assert plan["subtask_count"] == 1
    assert plan["has_contract"] is False


@pytest.mark.parametrize("artifact", [
    None,
    "not json",
    json.dumps({"short_summary": "missing required fields"}),
    json.dumps(_cross_plan(aliases=["api", "other"])),
])
def test_cross_plan_record_degrades_to_absent_for_invalid_artifact(
    tmp_path: Path, artifact: str | None,
) -> None:
    _write_run(tmp_path)
    if artifact is not None:
        (tmp_path / "cross_plan.json").write_text(artifact, encoding="utf-8")

    plan = collect_evidence(tmp_path)["plan"]

    assert plan["source"] == "absent"
    assert plan["short_summary"] == ""
    assert plan["planning_context"] == ""
    assert plan["subtask_count"] == 0
    assert plan["has_contract"] is False


def test_mono_plan_event_takes_precedence_over_cross_artifact(tmp_path: Path) -> None:
    mono_payload = {
        "source": "markdown",
        "short_summary": "Mono event remains authoritative.",
        "planning_context": "mono context",
        "subtask_count": 1,
        "has_contract": False,
        "goal": "Preserve mono behavior",
        "acceptance_criteria": [
            {"id": "C1", "intent": "event wins", "verify": "agent_assertion"},
        ],
        "owned_files": ["mono.py"],
        "commands_to_run": ["pytest -q mono"],
        "risks": ["cross override"],
        "review_focus": ["source priority"],
        "mcp_context": [{"name": "existing"}],
        "subtasks": [{"id": "mono", "goal": "keep event"}],
    }
    _write_run(tmp_path, events=[_plan_parsed_event(mono_payload)])
    _write_canonical_cross_plan(tmp_path)

    plan = collect_evidence(tmp_path)["plan"]

    assert plan == {
        **mono_payload,
        "mcp_context": [{"name": "existing"}],
        "subtasks": [{"id": "mono", "goal": "keep event"}],
    }
