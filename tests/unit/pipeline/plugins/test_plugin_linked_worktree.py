"""A linked git worktree inherits its repository's plugin.

``.orcho/`` is normally ignored or excluded from git, so a worktree created
with ``git worktree add`` never contains it — yet it is the same project
under the same verification contract. Reproduces the dogfood shape: the
project checkout registered with Orcho was a linked worktree of a repository
whose main working tree carried the contract; the run saw no plugin, declared
no gates, and its executable criteria had nothing to bind to.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline.plugins import PLUGIN_RELATIVE_PATH, load_plugin

pytestmark = [pytest.mark.git_worktree]

_PLUGIN = (
    "PLUGIN = {\n"
    "    'name': 'Main Tree Project',\n"
    "    'verification': {\n"
    "        'commands': {'unit': {'run': ['true']}},\n"
    "        'schedule': [{'after_phase': 'implement', 'commands': ['unit']}],\n"
    "    },\n"
    "}\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo_with_excluded_plugin(root: Path) -> Path:
    repo = root / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    plugin = repo / PLUGIN_RELATIVE_PATH
    plugin.parent.mkdir(parents=True)
    plugin.write_text(_PLUGIN, encoding="utf-8")
    # The dogfood shape: ``.orcho/`` kept out of git through info/exclude.
    (repo / ".git" / "info" / "exclude").write_text(".orcho/\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain") == ""
    return repo


def test_linked_worktree_inherits_the_main_tree_plugin(tmp_path: Path) -> None:
    repo = _repo_with_excluded_plugin(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))
    assert not (linked / PLUGIN_RELATIVE_PATH).exists()

    cfg = load_plugin(str(linked))

    assert cfg.name == "Main Tree Project"
    assert "unit" in cfg.verification["commands"]
    assert Path(cfg.loaded_plugin_path) == (repo / PLUGIN_RELATIVE_PATH).resolve()


def test_a_plugin_in_the_worktree_itself_wins(tmp_path: Path) -> None:
    repo = _repo_with_excluded_plugin(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))
    own = linked / PLUGIN_RELATIVE_PATH
    own.parent.mkdir(parents=True)
    own.write_text("PLUGIN = {'name': 'Worktree Override'}\n", encoding="utf-8")

    cfg = load_plugin(str(linked))

    assert cfg.name == "Worktree Override"
    assert Path(cfg.loaded_plugin_path) == own


def test_the_main_worktree_and_plain_directories_are_unchanged(tmp_path: Path) -> None:
    repo = _repo_with_excluded_plugin(tmp_path)
    assert load_plugin(str(repo)).name == "Main Tree Project"

    # A separate clone (own .git) that carries no plugin stays plugin-less.
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    assert not (clone / PLUGIN_RELATIVE_PATH).exists()
    assert load_plugin(str(clone)).loaded_plugin_path == ""

    plain = tmp_path / "plain"
    plain.mkdir()
    assert load_plugin(str(plain)).loaded_plugin_path == ""


# ── read-only git probe: every failure shape means "not a linked worktree" ─────


class _Result:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


@pytest.mark.parametrize(
    "outcome",
    [
        OSError("git missing"),
        _Result(128, ""),
        _Result(0, ".git\n"),
        _Result(0, "/elsewhere/worktrees/x\n/elsewhere/repo.bare\n"),
    ],
    ids=["oserror", "nonzero", "one-line", "bare-common-dir"],
)
def test_git_probe_failures_never_inherit(tmp_path: Path, monkeypatch, outcome) -> None:
    from pipeline import plugins

    def _fake_run(*_a, **_kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(plugins.subprocess, "run", _fake_run)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert plugins._linked_worktree_main_root(plain) is None
    assert load_plugin(str(plain)).loaded_plugin_path == ""


def test_git_probe_skips_a_missing_directory(tmp_path: Path) -> None:
    from pipeline import plugins

    assert plugins._linked_worktree_main_root(tmp_path / "absent") is None
