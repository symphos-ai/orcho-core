# SPDX-License-Identifier: Apache-2.0
"""Read-path projection of the durable ``diff_patch`` triad (T3).

``get_run_diff`` surfaces a human-readable patch-integrity advisory on the
existing ``RunDiffRecord.message`` field (no new wire field) when finalization
recorded the captured run patch as ``patch_invalid`` / ``patch_missing``. The
patch body (``content``) stays byte-faithful so ``git apply`` consumers are
unaffected; only ``message`` carries the warning.
"""
from __future__ import annotations

import json
from pathlib import Path

from sdk import get_run_diff

_PATCH = (
    "diff --git a/api/payload.py b/api/payload.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/api/payload.py\n"
    "+++ b/api/payload.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def _write_run(
    runs_dir: Path,
    run_id: str,
    *,
    diff_patch_block: dict | None,
    write_patch: bool = True,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    if write_patch:
        (run_dir / "diff.patch").write_text(_PATCH, encoding="utf-8")
    meta: dict = {"status": "done"}
    if diff_patch_block is not None:
        meta["diff_patch"] = diff_patch_block
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return run_dir


def test_read_path_warns_on_patch_invalid(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20260625_000001",
        diff_patch_block={
            "status": "patch_invalid",
            "reason": "patch_does_not_apply",
            "patch_path": "/runs/20260625_000001/diff.patch",
            "baseline_ref": "base-tree",
            "detail": "git apply --check exited with 1",
        },
    )
    rec = get_run_diff(
        "20260625_000001", runs_dir=tmp_path, cwd=None, mode="full",
    )

    assert rec.found is True
    # Body stays byte-faithful for git apply consumers.
    assert rec.content == _PATCH
    assert rec.message is not None
    assert rec.message.startswith("patch integrity")
    assert "patch_invalid" in rec.message
    assert "patch_does_not_apply" in rec.message
    assert "/runs/20260625_000001/diff.patch" in rec.message
    assert "recover from worktree or rerun" in rec.message


def test_read_path_warns_on_patch_missing(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20260625_000002",
        diff_patch_block={
            "status": "patch_missing",
            "reason": "patch_unreadable",
            "patch_path": "/runs/20260625_000002/diff.patch",
            "baseline_ref": "base-tree",
            "detail": "permission denied",
        },
    )
    rec = get_run_diff(
        "20260625_000002", runs_dir=tmp_path, cwd=None, mode="full",
    )

    assert rec.found is True
    assert rec.message is not None
    assert "patch_missing" in rec.message
    assert "patch_unreadable" in rec.message


def test_read_path_warns_when_artifact_missing(tmp_path: Path) -> None:
    # Real missing-artifact case (F1): finalization recorded patch_missing in
    # durable meta but diff.patch is absent on disk. found is False, yet the
    # operator must still see the recorded reason + path instead of a bare
    # "No diff artifact recorded" line.
    _write_run(
        tmp_path,
        "20260625_000005",
        write_patch=False,
        diff_patch_block={
            "status": "patch_missing",
            "reason": "patch_unavailable",
            "patch_path": "/runs/20260625_000005/diff.patch",
            "baseline_ref": "base-tree",
            "detail": "capture returned None",
        },
    )
    rec = get_run_diff(
        "20260625_000005", runs_dir=tmp_path, cwd=None, mode="full",
    )

    assert rec.found is False
    assert rec.message is not None
    assert rec.message.startswith("patch integrity")
    assert "patch_missing" in rec.message
    assert "patch_unavailable" in rec.message
    assert "/runs/20260625_000005/diff.patch" in rec.message
    assert "recover from worktree or rerun" in rec.message


def test_read_path_generic_message_when_missing_and_no_block(
    tmp_path: Path,
) -> None:
    # No durable block and no artifact: keep the plain "not recorded" line so a
    # clean run does not get a spurious integrity warning.
    _write_run(
        tmp_path,
        "20260625_000006",
        write_patch=False,
        diff_patch_block=None,
    )
    rec = get_run_diff(
        "20260625_000006", runs_dir=tmp_path, cwd=None, mode="full",
    )

    assert rec.found is False
    assert rec.message == "No diff artifact recorded for this run."


def test_read_path_quiet_for_valid_patch(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20260625_000003",
        diff_patch_block={
            "status": "patch_valid",
            "reason": "patch_applies",
            "patch_path": "/runs/20260625_000003/diff.patch",
            "baseline_ref": "base-tree",
            "detail": "",
        },
    )
    rec = get_run_diff(
        "20260625_000003", runs_dir=tmp_path, cwd=None, mode="full",
    )

    assert rec.found is True
    assert rec.message is None


def test_read_path_quiet_when_no_durable_block(tmp_path: Path) -> None:
    _write_run(tmp_path, "20260625_000004", diff_patch_block=None)
    rec = get_run_diff(
        "20260625_000004", runs_dir=tmp_path, cwd=None, mode="full",
    )

    assert rec.found is True
    assert rec.message is None
