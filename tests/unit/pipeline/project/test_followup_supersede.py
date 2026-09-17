"""``supersede_parent_after_child_delivery`` guards and effect."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.project.followup_supersede import (
    DELIVERED_STATUSES,
    supersede_parent_after_child_delivery,
)


def _lineage(
    tmp_path: Path, *, parent_halt: str | None = "commit_decision_fix",
    child_delivery: str | None = "committed", correction: bool = True,
) -> tuple[Path, dict]:
    runs = tmp_path / "runs"
    parent_dir = runs / "parent"
    child_dir = runs / "child"
    parent_dir.mkdir(parents=True)
    child_dir.mkdir(parents=True)
    parent: dict = {"status": "halted" if parent_halt else "done"}
    if parent_halt:
        parent["halt_reason"] = parent_halt
        parent["commit_delivery"] = {"status": "fix_requested", "action": "fix"}
    (parent_dir / "meta.json").write_text(json.dumps(parent), encoding="utf-8")
    child: dict = {"status": "done", "parent_run_id": "parent"}
    if correction:
        child.update({"resume_mode": "followup", "profile": "correction"})
        (child_dir / "correction_context.md").write_text("# ctx\n", encoding="utf-8")
    if child_delivery:
        child["commit_delivery"] = {"status": child_delivery}
    return runs, child


def _parent(runs: Path) -> dict:
    return json.loads((runs / "parent" / "meta.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("status", sorted(DELIVERED_STATUSES))
def test_delivered_child_supersedes_a_fix_parent(tmp_path: Path, status: str) -> None:
    runs, child = _lineage(tmp_path, child_delivery=status)
    assert supersede_parent_after_child_delivery(child, runs / "child", child_run_id="child") == "parent"
    parent = _parent(runs)
    assert parent["status"] == "done"
    assert "commit_delivery" not in parent
    assert parent["superseded_by_followup"] == {
        "child_run_id": "child", "child_status": "done",
        "delivery_status": status, "reason": "correction delivered via ordinary follow-up",
    }


def test_rejected_fa_parent_is_also_superseded(tmp_path: Path) -> None:
    runs, child = _lineage(tmp_path, parent_halt="final_acceptance_rejected")
    assert supersede_parent_after_child_delivery(child, runs / "child") == "parent"
    assert _parent(runs)["status"] == "done"


def test_parent_run_id_fallback_from_caller(tmp_path: Path) -> None:
    runs, child = _lineage(tmp_path)
    child.pop("parent_run_id")
    assert supersede_parent_after_child_delivery(child, runs / "child") is None
    assert supersede_parent_after_child_delivery(
        child, runs / "child", parent_run_id="parent",
    ) == "parent"


@pytest.mark.parametrize(
    "variant",
    ["undelivered", "pending", "not-correction", "parent-not-terminal", "no-run-dir", "no-parent-dir"],
)
def test_guards_are_silent_noops(tmp_path: Path, variant: str) -> None:
    kwargs: dict = {}
    if variant == "undelivered":
        kwargs["child_delivery"] = "not_applicable"
    elif variant == "pending":
        kwargs["child_delivery"] = "pending"
    elif variant == "not-correction":
        kwargs["correction"] = False
    elif variant == "parent-not-terminal":
        kwargs["parent_halt"] = "parse_error"
    runs, child = _lineage(tmp_path, **kwargs)
    if variant == "no-parent-dir":
        (runs / "parent" / "meta.json").unlink()
    run_dir = None if variant == "no-run-dir" else runs / "child"

    assert supersede_parent_after_child_delivery(child, run_dir) is None
    if variant != "no-parent-dir":
        assert "superseded_by_followup" not in _parent(runs)


def test_idempotent_on_rerun(tmp_path: Path) -> None:
    runs, child = _lineage(tmp_path)
    assert supersede_parent_after_child_delivery(child, runs / "child") == "parent"
    first = _parent(runs)
    assert supersede_parent_after_child_delivery(child, runs / "child") is None
    assert _parent(runs) == first
