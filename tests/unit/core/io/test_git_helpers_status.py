"""Exact working-tree status collection uses porcelain v1's NUL protocol."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.io.git_helpers import (
    GitStatusKind,
    GitStatusParseError,
    _parse_git_status_porcelain,
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
        SimpleNamespace(returncode=1, stdout=b""),
        FileNotFoundError("git"),
        OSError("unavailable cwd"),
        subprocess.TimeoutExpired(["git"], 30),
    ],
)
def test_git_changed_file_records_degrades_expected_invocation_failures(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    def run(*args: object, **kwargs: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("core.io.git_helpers.subprocess.run", run)

    assert git_changed_file_records("unused") == ()
    assert git_changed_files("unused") == []


def test_git_changed_file_records_uses_exact_binary_porcelain_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr("core.io.git_helpers.subprocess.run", run)

    assert git_changed_file_records("repo") == ()
    assert captured["args"] == (
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    assert captured["kwargs"] == {
        "cwd": "repo", "capture_output": True, "check": False, "timeout": 30.0,
    }


def test_git_changed_file_records_raises_for_malformed_successful_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.io.git_helpers.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"R  only-destination\0"),
    )

    with pytest.raises(GitStatusParseError):
        git_changed_file_records("unused")


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
