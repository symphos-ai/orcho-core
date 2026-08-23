"""Exact working-tree status collection uses porcelain v1's NUL protocol."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from core.io import git_helpers
from core.io.bounded_proc import Completed, SpawnFailure, TimedOut
from core.io.git_helpers import (
    GitStatusKind,
    GitStatusParseError,
    _parse_git_status_porcelain,
    _run_git,
    git_changed_file_records,
    git_changed_files,
)


@pytest.mark.parametrize(
    ("wire", "kind", "path", "old_path", "identities"),
    [
        (b" M modified.py\0", GitStatusKind.MODIFIED, "modified.py", None, ("modified.py",)),
        (b"A  added.py\0", GitStatusKind.ADDED, "added.py", None, ("added.py",)),
        (b"?? new.py\0", GitStatusKind.UNTRACKED, "new.py", None, ("new.py",)),
        (b" D removed.py\0", GitStatusKind.DELETED, "removed.py", None, ("removed.py",)),
        (b"R  destination.py\0source.py\0", GitStatusKind.RENAMED, "destination.py", "source.py", ("source.py", "destination.py")),
        (b" C destination.py\0source.py\0", GitStatusKind.COPIED, "destination.py", "source.py", ("source.py", "destination.py")),
    ],
)
def test_parse_status_records_preserves_exact_change_identity(
    wire: bytes,
    kind: GitStatusKind,
    path: str,
    old_path: str | None,
    identities: tuple[str, ...],
) -> None:
    (record,) = _parse_git_status_porcelain(wire)

    assert record.kind is kind
    assert record.path == path
    assert record.old_path == old_path
    assert record.scope_identities == identities


def test_parse_rename_uses_nul_porcelain_destination_then_source() -> None:
    records = _parse_git_status_porcelain(
        b"R  after -> literal.txt\0before -> literal.txt\0",
    )

    assert records[0].path == "after -> literal.txt"
    assert records[0].old_path == "before -> literal.txt"
    assert records[0].scope_identities == (
        "before -> literal.txt", "after -> literal.txt",
    )


@pytest.mark.parametrize(
    "wire",
    [b"M  no-terminator", b"R  destination\0", b"?? \0", b"bad\0"],
)
def test_parse_rejects_malformed_successful_output(wire: bytes) -> None:
    with pytest.raises(GitStatusParseError):
        _parse_git_status_porcelain(wire)


def test_git_changed_files_deduplicates_rename_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.io.git_helpers.git_changed_file_records",
        lambda cwd: _parse_git_status_porcelain(
            b"R  destination.py\0source.py\0 M destination.py\0",
        ),
    )

    assert git_changed_files("unused") == ["source.py", "destination.py"]


@pytest.mark.parametrize(
    "outcome",
    [
        Completed(1, b"", b""),
        SpawnFailure("git", b"", b""),
        TimedOut(b"", b"", None, True),
    ],
)
def test_git_changed_file_records_degrades_expected_invocation_failures(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    monkeypatch.setattr("core.io.git_helpers.run_bounded", lambda *args, **kwargs: outcome)

    assert git_changed_file_records("unused") == ()
    assert git_changed_files("unused") == []


def test_git_changed_file_records_uses_exact_binary_porcelain_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> Completed:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Completed(0, b"", b"")

    monkeypatch.setattr("core.io.git_helpers.run_bounded", run)

    assert git_changed_file_records("repo") == ()
    assert captured["args"] == (
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    assert captured["kwargs"] == {
        "cwd": "repo", "timeout_s": 30.0, "reap_budget_s": 1.0,
    }


def test_git_changed_file_records_raises_for_malformed_successful_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.io.git_helpers.run_bounded",
        lambda *args, **kwargs: Completed(0, b"R  only-destination\0", b""),
    )

    with pytest.raises(GitStatusParseError):
        git_changed_file_records("unused")


def test_run_git_preserves_missing_binary_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = FileNotFoundError("git")
    monkeypatch.setattr(
        "core.io.git_helpers.run_bounded",
        lambda *args, **kwargs: SpawnFailure(str(missing), b"", b"", missing),
    )

    assert _run_git(["rev-parse", "HEAD"]) == (
        -1, "", "git binary not found: git",
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)


def test_git_backed_status_reports_each_nested_untracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "nested").mkdir()
    (repo / "nested" / "one.txt").write_text("one", encoding="utf-8")
    (repo / "nested" / "two.txt").write_text("two", encoding="utf-8")

    assert git_changed_files(repo) == ["nested/one.txt", "nested/two.txt"]


def test_git_backed_status_round_trips_quoting_sensitive_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    names = ["space name.txt", "žlutý.txt", 'quote"name.txt', "literal -> arrow.txt"]
    for name in names:
        (repo / name).write_text("new", encoding="utf-8")

    assert git_changed_files(repo) == sorted(names)


def test_git_backed_rename_reports_source_and_destination_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").rename(repo / "renamed.txt")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    assert git_changed_files(repo) == ["tracked.txt", "renamed.txt"]


def test_text_mode_git_calls_pin_utf8_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    # git emits raw UTF-8 pathnames; decoding with the process locale breaks
    # on non-UTF-8 Windows codepages. Every text-mode git call in this module
    # must pin the codec instead of inheriting the locale.
    from core.io import git_helpers

    seen: list[tuple[object, dict[str, object]]] = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return Completed(0, "", "")

    monkeypatch.setattr(git_helpers, "run_bounded", fake_run)
    git_helpers.has_uncommitted(".")
    git_helpers.git_diff_stat(".")
    git_helpers._run_git(["rev-parse", "HEAD"], cwd=".")
    git_helpers.apply_patch_to_checkout(
        checkout_path=Path("."), patch_text="diff --git a/x b/x\n",
    )

    assert len(seen) == 4
    assert [cmd for cmd, _kwargs in seen] == [
        ["git", "status", "--porcelain"],
        ["git", "diff", "--stat"],
        ["git", "rev-parse", "HEAD"],
        ["git", "apply", "-"],
    ]
    assert [kwargs["cwd"] for _cmd, kwargs in seen] == [".", ".", ".", "."]
    assert seen[-1][1]["input_data"] == "diff --git a/x b/x\n"
    for _cmd, kwargs in seen:
        assert kwargs.get("text") is True
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable git shim")
def test_stalled_git_degrades_status_helpers_within_short_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    git = tmp_path / "git"
    # Every path in the stub must be absolute. PATH is stripped to this
    # directory below, so a ``#!/usr/bin/env python3`` shebang or a bare
    # ``sleep`` cannot be resolved: the stub would exit 127 at once and the
    # test would be measuring process startup latency, not a stall — passing
    # or failing on whether a cold spawn happened to exceed the ceiling.
    git.write_text("#!/bin/sh\nexec /bin/sleep 30\n", encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("core.io.git_helpers._GIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr("core.io.git_helpers._GIT_REAP_BUDGET_S", 0.1)

    started = time.monotonic()
    # ``git_changed_file_records`` owns a documented degrade-to-empty contract.
    assert git_changed_file_records(str(tmp_path)) == ()
    # The working-tree questions do not: a stall is an unknown answer, never
    # a clean tree. Both must still return inside the declared ceiling.
    with pytest.raises(TimeoutError):
        git_helpers.has_uncommitted(str(tmp_path))
    with pytest.raises(TimeoutError):
        git_helpers.git_diff_stat(str(tmp_path))
    assert time.monotonic() - started < 1.0


class TestWorkingTreeQuestionsStayHonest:
    """``has_uncommitted`` / ``git_diff_stat`` answer a question about the
    working tree. "git could not be consulted" is not the answer "clean" —
    downstream that reads as "no file changes were produced" and lets final
    acceptance approve an empty diff surface."""

    def test_unusable_cwd_raises_instead_of_reporting_clean(self, tmp_path) -> None:
        missing = tmp_path / "not-a-directory"

        with pytest.raises(OSError):
            git_helpers.has_uncommitted(str(missing))
        with pytest.raises(OSError):
            git_helpers.git_diff_stat(str(missing))

    def test_stalled_git_raises_instead_of_reporting_clean(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            git_helpers, "run_bounded",
            lambda *args, **kwargs: TimedOut(
                stdout="", stderr="", returncode=None, reap_exhausted=True,
            ),
        )

        with pytest.raises(TimeoutError):
            git_helpers.has_uncommitted(str(tmp_path))
        with pytest.raises(TimeoutError):
            git_helpers.git_diff_stat(str(tmp_path))
