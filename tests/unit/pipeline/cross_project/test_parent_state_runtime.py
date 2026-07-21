"""Runtime adapter shares the disk adapter's canonical fact reduction."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.cross_project.parent_state_runtime import reduce_runtime_cross_parent_state
from pipeline.run_state.cross_parent import ParentClass
from pipeline.run_state.cross_parent_disk import load_cross_parent_state


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_runtime_and_disk_reductions_match_equivalent_facts(tmp_path: Path) -> None:
    parent = {
        "status": "done",
        "projects": {"api": "/api"},
        "phases": {"projects": {"api": {"status": "done"}}},
    }
    _write(tmp_path / "meta.json", parent)
    _write(tmp_path / "api" / "meta.json", {"status": "done"})

    disk = load_cross_parent_state(tmp_path)
    runtime = reduce_runtime_cross_parent_state(parent, {}, tmp_path)

    assert runtime == disk


def test_runtime_embedded_snapshot_cannot_hide_physical_conflict(tmp_path: Path) -> None:
    session = {"projects": {"api": "/api"}, "phases": {"projects": {"api": {"status": "done"}}}}
    _write(tmp_path / "meta.json", {"projects": {"api": "/api"}})
    _write(tmp_path / "api" / "meta.json", {"status": "failed"})

    state = reduce_runtime_cross_parent_state(session, {}, tmp_path)

    assert "embedded_physical_status_conflict" in {item.code for item in state.violations}


def test_runtime_embedded_done_cannot_replace_missing_physical_child(tmp_path: Path) -> None:
    session = {"projects": {"api": "/api"}, "phases": {"projects": {"api": {"status": "done"}}}}
    _write(tmp_path / "meta.json", {"projects": {"api": "/api"}})

    state = reduce_runtime_cross_parent_state(session, {"sub_status": {"api": "done"}}, tmp_path)

    assert state.children[0].contract_evaluable is False
    assert state.children[0].execution.value == "pending"
    assert state.parent_class is ParentClass.INCONSISTENT
    assert "embedded_without_physical" in {item.code for item in state.violations}
