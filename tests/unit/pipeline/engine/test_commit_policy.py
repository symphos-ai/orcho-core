"""The delivery-policy snapshot a parked gate pins (``commit_policy``)."""
from __future__ import annotations

import pytest

from pipeline.engine.commit_policy import (
    COMMIT_POLICY_KEYS,
    normalize_commit_message_strategy,
    overlay_commit_policy,
    snapshot_commit_policy,
)


def test_snapshot_normalises_the_three_policy_axes() -> None:
    snap = snapshot_commit_policy(
        {"branch_policy": "bypass", "publish": "OFF", "default_strategy": "llm_generate"},
    )
    assert snap == {
        "branch_policy": "bypass",
        "publish": "off",
        "default_strategy": "llm_generate",
    }


def test_snapshot_of_defaults_uses_the_engine_defaults() -> None:
    assert snapshot_commit_policy({}) == {
        "branch_policy": "worktree_branch",
        "publish": "auto",
        "default_strategy": "release_summary",
    }
    assert snapshot_commit_policy(None) == snapshot_commit_policy({})


def test_snapshot_degrades_unknown_values_like_the_engine() -> None:
    snap = snapshot_commit_policy(
        {"branch_policy": "yolo", "publish": 3, "default_strategy": "poem"},
    )
    assert snap["branch_policy"] == "worktree_branch"
    assert snap["publish"] == "auto"
    assert snap["default_strategy"] == "release_summary"


def test_snapshot_keeps_optional_names_only_when_set() -> None:
    snap = snapshot_commit_policy(
        {"branch_name": " release/x ", "publish_provider": "", "decision_mode": "defer"},
    )
    assert snap["branch_name"] == "release/x"
    assert "publish_provider" not in snap
    assert "decision_mode" not in snap
    assert set(snap) <= set(COMMIT_POLICY_KEYS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("llm_generate", "llm_generate"), ("operator_typed", "operator_typed"),
     (None, "release_summary"), ("", "release_summary"), ("x", "release_summary")],
)
def test_normalize_strategy(raw, expected) -> None:
    assert normalize_commit_message_strategy(raw) == expected


def test_overlay_pins_only_policy_keys() -> None:
    base = {"branch_policy": "worktree_branch", "publish": "always",
            "add_untracked": True, "enabled": True, "auto_in_ci": "approve"}
    out = overlay_commit_policy(base, {"branch_policy": "bypass", "publish": "off",
                                       "add_untracked": False, "enabled": False})
    assert out["branch_policy"] == "bypass"
    assert out["publish"] == "off"
    # Non-policy keys are never taken from the snapshot.
    assert out["add_untracked"] is True
    assert out["enabled"] is True
    assert out["auto_in_ci"] == "approve"


def test_overlay_without_snapshot_is_a_copy_of_base() -> None:
    base = {"branch_policy": "worktree_branch"}
    for snapshot in (None, {}, "bypass", 7):
        out = overlay_commit_policy(base, snapshot)
        assert out == base
        assert out is not base
