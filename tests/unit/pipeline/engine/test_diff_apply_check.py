# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.engine.diff_apply_check import (
    DiffApplyCheckResult,
    check_diff_patch_apply,
    diff_patch_durable_block,
    diff_patch_triad,
)


def _require_git() -> None:
    """Skip (not fail) when no git binary is present.

    A degraded check with no git available is a legitimate ``git_unavailable``
    outcome, so the environment lacking git must not turn these tests red.
    """
    if shutil.which("git") is None:
        pytest.skip("git binary not available")


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _git_output(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")
    (path / "payload.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "payload.py")
    _git(path, "commit", "-qm", "initial")


def test_check_diff_patch_apply_passes_against_baseline_without_mutating_checkout(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _init_repo(project)
    baseline = _git_output(project, "rev-parse", "HEAD").strip()
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text(
        _git_output(project, "diff", "--binary", "HEAD", "--", "payload.py"),
        encoding="utf-8",
    )

    result = check_diff_patch_apply(
        project,
        patch_path=patch_path,
        baseline_ref=baseline,
    )

    assert result.status == "pass"
    assert result.reason == "patch_applies"
    assert result.baseline_ref == baseline
    assert result.command == (
        "git",
        "apply",
        "--check",
        "--cached",
        str(patch_path),
    )
    assert _git_output(project, "diff", "--", "payload.py")


def test_check_diff_patch_apply_fails_corrupt_patch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _init_repo(project)
    baseline = _git_output(project, "rev-parse", "HEAD").strip()
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text("not a unified patch\n", encoding="utf-8")

    result = check_diff_patch_apply(
        project,
        patch_path=patch_path,
        baseline_ref=baseline,
    )

    assert result.status == "fail"
    assert result.reason == "patch_does_not_apply"
    assert result.detail == "git apply --check exited with 128"
    assert diff_patch_triad(result) == "patch_invalid"


def test_check_diff_patch_apply_degrades_on_empty_baseline(tmp_path: Path) -> None:
    """A *valid* patch with an empty baseline degrades — it must NOT fail.

    This is the direct review question: an unavailable baseline ('' / blank)
    is degraded/``patch_unknown``, distinct from a corrupt patch which is
    ``fail``/``patch_invalid``. The patch here genuinely applies against HEAD,
    so any 'pass' or 'fail' would mean the empty-baseline guard regressed.
    """
    _require_git()
    project = tmp_path / "project"
    _init_repo(project)
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text(
        _git_output(project, "diff", "--binary", "HEAD", "--", "payload.py"),
        encoding="utf-8",
    )

    result = check_diff_patch_apply(
        project,
        patch_path=patch_path,
        baseline_ref="   ",
    )

    assert result.status == "degraded"
    assert result.status != "pass"
    assert result.reason == "baseline_unavailable"
    assert diff_patch_triad(result) == "patch_unknown"


def test_check_diff_patch_apply_degrades_when_git_root_unresolvable(
    tmp_path: Path,
) -> None:
    """A non-empty baseline but no resolvable git root degrades, never passes.

    ``project_path`` points at a directory with no ``.git``; resolution is
    filesystem-based (no git binary), so this case needs no git skip-guard.
    The patch is readable and well-formed, so the only reason for degraded
    here is the unresolvable git root.
    """
    non_git = tmp_path / "no-git"
    non_git.mkdir()
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text(
        "diff --git a/payload.py b/payload.py\n"
        "--- a/payload.py\n"
        "+++ b/payload.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n",
        encoding="utf-8",
    )

    result = check_diff_patch_apply(
        non_git,
        patch_path=patch_path,
        baseline_ref="deadbeef",
    )

    assert result.status == "degraded"
    assert result.status != "pass"
    assert result.reason == "git_root_unavailable"
    assert diff_patch_triad(result) == "patch_unknown"


def test_check_diff_patch_apply_degrades_when_patch_missing(tmp_path: Path) -> None:
    """A missing patch artifact degrades with the ``patch_missing`` triad.

    Distinct from the ``patch_unknown`` baseline/git-root degrades: an absent
    artifact projects to ``patch_missing``. No git binary is needed because the
    is-file guard short-circuits before any git command runs.
    """
    missing_patch = tmp_path / "absent.patch"

    result = check_diff_patch_apply(
        tmp_path / "project",
        patch_path=missing_patch,
        baseline_ref="deadbeef",
    )

    assert result.status == "degraded"
    assert result.status != "pass"
    assert result.reason == "patch_unavailable"
    assert diff_patch_triad(result) == "patch_missing"


def test_diff_patch_durable_block_keeps_only_actionable_fields() -> None:
    result = DiffApplyCheckResult(
        status="fail",
        reason="patch_does_not_apply",
        cwd="/repo",
        patch_path="/runs/run/diff.patch",
        baseline_ref="base",
        command=("git", "apply", "--check"),
        stdout="ignored",
        stderr="ignored",
        detail="git apply --check exited with 1",
        stdout_truncated=True,
        stderr_truncated=True,
    )

    assert diff_patch_durable_block(result) == {
        "status": "patch_invalid",
        "reason": "patch_does_not_apply",
        "patch_path": "/runs/run/diff.patch",
        "baseline_ref": "base",
        "detail": "git apply --check exited with 1",
    }
