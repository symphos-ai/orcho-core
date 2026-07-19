from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from pipeline.verification_subject import (
    VerificationSubjectAvailable,
    VerificationSubjectComparisonVerdict,
    VerificationSubjectUnavailable,
    capture_verification_subject,
    compare_verification_subjects,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")
    (path / "payload.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "payload.txt")
    _git(path, "commit", "-qm", "initial")


def _identity(path: Path):
    captured = capture_verification_subject(path, baseline_ref="HEAD")
    assert isinstance(captured, VerificationSubjectAvailable)
    return captured.identity


def _tree_entry(path: Path, tree_oid: str, name: str) -> str:
    return _git(path, "ls-tree", tree_oid, "--", name)


def test_capture_clean_identity_and_direct_comparison(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    first = _identity(tmp_path)
    second = _identity(tmp_path)

    assert first.object_format in {"sha1", "sha256"}
    assert first.tree_oid == _git(tmp_path, "rev-parse", "HEAD^{tree}")
    assert first.observed_head_oid == _git(tmp_path, "rev-parse", "HEAD")
    assert first.baseline_oid == first.observed_head_oid
    assert compare_verification_subjects(first, second).verdict is VerificationSubjectComparisonVerdict.FRESH


def test_same_path_content_change_changes_tree_with_same_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    recorded = _identity(tmp_path)
    (tmp_path / "payload.txt").write_text("two\n", encoding="utf-8")
    current = _identity(tmp_path)

    assert recorded.observed_head_oid == current.observed_head_oid
    assert recorded.tree_oid != current.tree_oid
    assert compare_verification_subjects(recorded, current).verdict is VerificationSubjectComparisonVerdict.STALE


def test_capture_is_non_mutating_for_index_refs_and_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "tracked-ignored.txt").write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked-ignored.txt")
    _git(tmp_path, "commit", "-qm", "ignored tracked")
    (tmp_path / ".gitignore").write_text("tracked-ignored.txt\nignored.txt\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-qm", "ignore tracked path")
    (tmp_path / "tracked-ignored.txt").write_text("after\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignore me\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("include me\n", encoding="utf-8")
    index_before = (tmp_path / ".git" / "index").read_bytes()
    head_before = _git(tmp_path, "rev-parse", "HEAD")
    refs_before = _git(tmp_path, "for-each-ref")
    status_before = _git(tmp_path, "status", "--porcelain=v1")

    identity = _identity(tmp_path)

    assert (tmp_path / ".git" / "index").read_bytes() == index_before
    assert _git(tmp_path, "rev-parse", "HEAD") == head_before
    assert _git(tmp_path, "for-each-ref") == refs_before
    assert _git(tmp_path, "status", "--porcelain=v1") == status_before
    assert (tmp_path / "tracked-ignored.txt").read_text(encoding="utf-8") == "after\n"
    names = _git(tmp_path, "ls-tree", "-r", "--name-only", identity.tree_oid).splitlines()
    assert "tracked-ignored.txt" in names
    assert "new.txt" in names
    assert "ignored.txt" not in names


def test_tracked_addition_changes_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = _identity(tmp_path)
    (tmp_path / "added.txt").write_text("added\n", encoding="utf-8")
    after = _identity(tmp_path)

    assert before.tree_oid != after.tree_oid
    assert "added.txt" in _git(tmp_path, "ls-tree", "-r", "--name-only", after.tree_oid)


def test_tracked_deletion_changes_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = _identity(tmp_path)
    (tmp_path / "payload.txt").unlink()
    after = _identity(tmp_path)

    assert before.tree_oid != after.tree_oid
    assert "payload.txt" not in _git(tmp_path, "ls-tree", "-r", "--name-only", after.tree_oid)


def test_rename_changes_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = _identity(tmp_path)
    (tmp_path / "payload.txt").rename(tmp_path / "renamed.txt")
    after = _identity(tmp_path)

    assert before.tree_oid != after.tree_oid
    assert "renamed.txt" in _git(tmp_path, "ls-tree", "-r", "--name-only", after.tree_oid)


def test_executable_mode_changes_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = _identity(tmp_path)
    (tmp_path / "payload.txt").chmod(0o755)
    after = _identity(tmp_path)

    assert before.tree_oid != after.tree_oid
    assert _tree_entry(tmp_path, after.tree_oid, "payload.txt").startswith("100755 ")


def test_symlink_target_changes_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    link = tmp_path / "link"
    link.symlink_to("payload.txt")
    before = _identity(tmp_path)
    link.unlink()
    link.symlink_to("other.txt")
    after = _identity(tmp_path)

    assert before.tree_oid != after.tree_oid
    assert _tree_entry(tmp_path, after.tree_oid, "link").startswith("120000 ")


def test_non_ignored_untracked_content_changes_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = _identity(tmp_path)
    (tmp_path / "untracked.txt").write_text("first\n", encoding="utf-8")
    first = _identity(tmp_path)
    (tmp_path / "untracked.txt").write_text("second\n", encoding="utf-8")
    second = _identity(tmp_path)

    assert before.tree_oid != first.tree_oid != second.tree_oid


def test_ignored_untracked_content_does_not_change_subject_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-qm", "ignore")
    before = _identity(tmp_path)
    (tmp_path / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    after = _identity(tmp_path)

    assert before.tree_oid == after.tree_oid


def test_dirty_submodule_superproject_is_unavailable_but_submodule_is_captured(tmp_path: Path) -> None:
    child = tmp_path / "child"
    _init_repo(child)
    superproject = tmp_path / "super"
    _init_repo(superproject)
    _git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested")
    _git(superproject, "commit", "-qm", "add nested")
    (superproject / "nested" / "payload.txt").write_text("dirty\n", encoding="utf-8")

    nested = capture_verification_subject(superproject / "nested")
    parent = capture_verification_subject(superproject)

    assert isinstance(nested, VerificationSubjectAvailable)
    assert isinstance(parent, VerificationSubjectUnavailable)
    assert parent.reason == "dirty_submodule_unrepresentable"


def test_dirty_submodule_with_non_ascii_path_is_unavailable(tmp_path: Path) -> None:
    child = tmp_path / "child"
    _init_repo(child)
    superproject = tmp_path / "super"
    _init_repo(superproject)
    _git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "модуль")
    _git(superproject, "commit", "-qm", "add non-ascii nested")
    (superproject / "модуль" / "payload.txt").write_text("dirty\n", encoding="utf-8")

    parent = capture_verification_subject(superproject)

    assert isinstance(parent, VerificationSubjectUnavailable)
    assert parent.reason == "dirty_submodule_unrepresentable"


def test_capture_unavailable_for_non_repo_or_git_failure(tmp_path: Path) -> None:
    assert isinstance(capture_verification_subject(tmp_path), VerificationSubjectUnavailable)
    _init_repo(tmp_path / "repo")
    with patch("pipeline.verification_subject._run_git", return_value=None):
        unavailable = capture_verification_subject(tmp_path / "repo")
    assert isinstance(unavailable, VerificationSubjectUnavailable)


def test_capture_unavailable_when_temporary_index_cannot_be_created(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with patch("pipeline.verification_subject.tempfile.mkdtemp", side_effect=OSError):
        unavailable = capture_verification_subject(tmp_path)

    assert isinstance(unavailable, VerificationSubjectUnavailable)
    assert unavailable.reason == "worktree_snapshot_unavailable"
