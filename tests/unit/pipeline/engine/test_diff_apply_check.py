# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import pipeline.engine.diff_apply_check as dac
from pipeline.engine.diff_apply_check import (
    DiffApplyCheckResult,
    _bound_text,
    _coerce_output,
    _command_label,
    _display_path,
    _GitCommandResult,
    _resolve_root,
    _run_git_command,
    capture_run_diff_with_apply_check,
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


# --- capture_run_diff_with_apply_check: no-diff -> None (line 160) ----------


@pytest.mark.git_worktree
@pytest.mark.serial
def test_capture_run_diff_with_apply_check_returns_none_without_changes(
    tmp_path: Path,
) -> None:
    """Line 160: a clean repo (no diff vs HEAD) yields ``None``."""
    _require_git()
    project = tmp_path / "project"
    _init_repo(project)
    baseline = _git_output(project, "rev-parse", "HEAD").strip()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    captured = capture_run_diff_with_apply_check(
        project, run_dir, baseline_ref=baseline,
    )

    assert captured is None
    assert not (run_dir / "diff.patch").exists()


# --- capture_run_diff_with_apply_check: emit failure swallowed (369-370) ----


@pytest.mark.git_worktree
@pytest.mark.serial
def test_capture_run_diff_with_apply_check_survives_emit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 369-370 (+162-171): a raising event emitter is swallowed."""
    _require_git()
    project = tmp_path / "project"
    _init_repo(project)
    baseline = _git_output(project, "rev-parse", "HEAD").strip()
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    from core.observability import events as _events

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("emit exploded")

    monkeypatch.setattr(_events, "emit", _boom)

    captured = capture_run_diff_with_apply_check(
        project, run_dir, baseline_ref=baseline,
    )

    assert captured is not None
    assert captured.path == run_dir / "diff.patch"
    assert captured.apply_check is not None
    assert captured.apply_check.status == "pass"


# --- check_diff_patch_apply: patch present but unreadable (213-214) ---------


def test_check_diff_patch_apply_degrades_when_patch_unreadable(
    tmp_path: Path,
) -> None:
    """Lines 213-214: a regular file that cannot be read -> patch_unreadable."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses file-permission read errors")
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text("data\n", encoding="utf-8")
    patch_path.chmod(0o000)
    try:
        result = check_diff_patch_apply(
            tmp_path / "project",
            patch_path=patch_path,
            baseline_ref="deadbeef",
        )
    finally:
        patch_path.chmod(0o600)

    assert result.status == "degraded"
    assert result.reason == "patch_unreadable"
    assert diff_patch_triad(result) == "patch_missing"


# --- read-tree returncode is None -> degraded (261-266) --------------------


def _readable_patch(tmp_path: Path) -> Path:
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text(
        "diff --git a/payload.py b/payload.py\n"
        "--- a/payload.py\n+++ b/payload.py\n"
        "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
        encoding="utf-8",
    )
    return patch_path


@pytest.mark.parametrize(
    ("unavailable_reason", "expected"),
    [
        ("git_unavailable", "git_unavailable"),
        ("timeout", "baseline_unavailable"),
    ],
)
def test_read_tree_returncode_none_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable_reason: str,
    expected: str,
) -> None:
    """Lines 261-266: read-tree returncode None maps to the right reason."""
    patch_path = _readable_patch(tmp_path)
    git_root = tmp_path / "root"
    git_root.mkdir()

    def _fake(command, **kwargs):
        return _GitCommandResult(
            returncode=None, unavailable_reason=unavailable_reason,
        )

    monkeypatch.setattr(dac, "_run_git_command", _fake)

    result = check_diff_patch_apply(
        git_root=git_root,
        patch_path=patch_path,
        baseline_ref="deadbeef",
    )

    assert result.status == "degraded"
    assert result.reason == expected
    assert result.command == ("git", "read-tree", "deadbeef")


# --- apply returncode is None -> degraded (295-300) ------------------------


@pytest.mark.parametrize(
    ("unavailable_reason", "expected"),
    [
        ("git_unavailable", "git_unavailable"),
        ("timeout", "apply_check_unavailable"),
    ],
)
def test_apply_returncode_none_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable_reason: str,
    expected: str,
) -> None:
    """Lines 295-300: read-tree ok but apply returncode None degrades."""
    patch_path = _readable_patch(tmp_path)
    git_root = tmp_path / "root"
    git_root.mkdir()

    def _fake(command, **kwargs):
        if command[1] == "read-tree":
            return _GitCommandResult(returncode=0)
        return _GitCommandResult(
            returncode=None, unavailable_reason=unavailable_reason,
        )

    monkeypatch.setattr(dac, "_run_git_command", _fake)

    result = check_diff_patch_apply(
        git_root=git_root,
        patch_path=patch_path,
        baseline_ref="deadbeef",
    )

    assert result.status == "degraded"
    assert result.reason == expected
    assert result.command == ("git", "apply", "--check", "--cached",
                              str(patch_path.resolve()))


# --- _resolve_root branches (346-347, 349) ---------------------------------


def test_resolve_root_returns_existing_git_root(tmp_path: Path) -> None:
    """Line 346: an existing git_root path is returned verbatim."""
    git_root = tmp_path / "root"
    git_root.mkdir()

    assert _resolve_root(project_path=None, git_root=git_root) == git_root


def test_resolve_root_missing_git_root_is_none(tmp_path: Path) -> None:
    """Line 347: a non-existent git_root resolves to None."""
    assert _resolve_root(
        project_path=None, git_root=tmp_path / "absent",
    ) is None


def test_resolve_root_without_any_input_is_none() -> None:
    """Line 349: no project_path and no git_root resolves to None."""
    assert _resolve_root(project_path=None, git_root=None) is None


def test_check_diff_patch_apply_degrades_when_git_root_missing(
    tmp_path: Path,
) -> None:
    """Line 347 via public API: a missing git_root -> git_root_unavailable."""
    patch_path = _readable_patch(tmp_path)

    result = check_diff_patch_apply(
        git_root=tmp_path / "absent",
        patch_path=patch_path,
        baseline_ref="deadbeef",
    )

    assert result.status == "degraded"
    assert result.reason == "git_root_unavailable"


# --- _run_git_command subprocess failure branches (391-410) ----------------


def test_run_git_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 391-398: TimeoutExpired -> returncode None, reason 'timeout'."""
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="git read-tree", timeout=5, output=b"out", stderr=b"err",
        )

    monkeypatch.setattr(dac.subprocess, "run", _raise)

    result = _run_git_command(
        ("git", "read-tree", "base"),
        cwd=str(tmp_path), env={}, timeout=5,
    )

    assert result.returncode is None
    assert result.unavailable_reason == "timeout"
    assert "timed out" in result.detail
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_run_git_command_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 399-404: FileNotFoundError -> reason 'git_unavailable'."""
    def _raise(*args, **kwargs):
        raise FileNotFoundError("no git here")

    monkeypatch.setattr(dac.subprocess, "run", _raise)

    result = _run_git_command(
        ("git", "read-tree", "base"),
        cwd=str(tmp_path), env={}, timeout=5,
    )

    assert result.returncode is None
    assert result.unavailable_reason == "git_unavailable"
    assert "no git here" in result.detail


def test_run_git_command_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 405-410: a generic OSError -> reason 'os_error'."""
    def _raise(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(dac.subprocess, "run", _raise)

    result = _run_git_command(
        ("git", "read-tree", "base"),
        cwd=str(tmp_path), env={}, timeout=5,
    )

    assert result.returncode is None
    assert result.unavailable_reason == "os_error"
    assert "disk gone" in result.detail


# --- trivial helper direct calls (451, 455, 459-463, 467-469, 475-476) -----


def test_bound_text_zero_max_bytes() -> None:
    """Line 451: max_bytes<=0 returns empty text with truncated flag."""
    assert _bound_text("x", 0) == ("", True)
    assert _bound_text("", 0) == ("", False)


def test_bound_text_truncates_overlong_text() -> None:
    """Line 455: text longer than max_bytes is truncated with the flag set."""
    bounded, truncated = _bound_text("abcdef", 3)

    assert truncated is True
    assert bounded == "abc"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (b"bytes", "bytes"),
        ("plain", "plain"),
    ],
)
def test_coerce_output_variants(value, expected) -> None:
    """Lines 459-463: None/bytes/str outputs are normalised to str."""
    assert _coerce_output(value) == expected


def test_command_label_single_and_empty() -> None:
    """Lines 467-469: a 1-element label joins, an empty tuple falls back."""
    assert _command_label(("git",)) == "git"
    assert _command_label(()) == "command"


def test_display_path_falls_back_on_resolve_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 475-476: a resolve() OSError falls back to str(path)."""
    target = tmp_path / "diff.patch"

    def _boom(self, *args, **kwargs):
        raise OSError("cannot resolve")

    monkeypatch.setattr(Path, "resolve", _boom)

    assert _display_path(target) == str(target)
