"""Public read-only loader for canonical cross-parent state."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.cross_project.parent_state_runtime import reduce_runtime_cross_parent_state
from sdk import CrossParentState, load_cross_parent_state, to_jsonable


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_loader_resolves_runs_dir_is_typed_jsonable_and_read_only(tmp_path: Path) -> None:
    run = tmp_path / "cross-1"
    parent = {
        "status": "done",
        "projects": {"api": "/api"},
        "phases": {"projects": {"api": {"status": "done"}}},
    }
    _write(run / "meta.json", parent)
    _write(run / "api" / "meta.json", {"status": "done"})
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}

    state = load_cross_parent_state("cross-1", runs_dir=tmp_path, cwd=None)

    assert isinstance(state, CrossParentState)
    assert to_jsonable(state)["parent_class"] == "terminal_success"
    after = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    assert after == before


def test_loader_matches_runtime_adapter_and_preserves_malformed_facts(tmp_path: Path) -> None:
    run = tmp_path / "cross-1"
    session = {"projects": {"api": "/api"}, "phases": {"projects": {"api": {"status": "done"}}}}
    _write(run / "meta.json", {"projects": {"api": "/api"}})
    (run / "api").mkdir()
    (run / "api" / "meta.json").write_text("{broken", encoding="utf-8")

    sdk_state = load_cross_parent_state("cross-1", runs_dir=tmp_path)
    runtime_state = reduce_runtime_cross_parent_state(session, {}, run)

    assert sdk_state.parent_class.value == "inconsistent"
    assert runtime_state.parent_class.value == "inconsistent"


def test_loader_accepts_durable_project_proxy_and_child_handoff_pair(tmp_path: Path) -> None:
    run = tmp_path / "cross-1"
    _write(
        run / "meta.json",
        {
            "projects": {"api": "/api"},
            "phase_handoff": {
                "id": "parent-handoff",
                "project_alias": "api",
                "child_handoff_id": "child-handoff",
                "available_actions": ["continue"],
            },
        },
    )
    _write(
        run / "api" / "meta.json",
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "child-handoff", "available_actions": ["continue"]},
        },
    )
    _write(
        run / "cross_checkpoint.json",
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "parent-handoff",
            "phase_handoff_project_alias": "api",
            "phase_handoff_child_id": "child-handoff",
        },
    )

    state = load_cross_parent_state("cross-1", runs_dir=tmp_path, cwd=None)

    assert state.parent_class.value == "awaiting_operator"
    assert state.violations == ()
