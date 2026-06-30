"""Unit tests for pipeline.project.project_discovery_prompt."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from pipeline.project.project_discovery_prompt import prompt_for_extra_projects
from sdk.workspace import ExtraProject, UndetectedCandidate


def _make_candidate(
    name: str = "myproject",
    path: str = "/tmp/myproject",
    nested: tuple[str, ...] = (),
) -> UndetectedCandidate:
    return UndetectedCandidate(name=name, path=path, nested_git_dirs=nested)


def _run_prompt(
    candidates: list[UndetectedCandidate],
    user_input: str,
) -> list[ExtraProject]:
    stdin = io.StringIO(user_input)
    stdout = io.StringIO()
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    stdout.isatty = lambda: False  # type: ignore[method-assign]
    return prompt_for_extra_projects(candidates, stdin=stdin, stdout=stdout)


class TestYNGating:
    def test_n_answer_skips_registration(self) -> None:
        candidates = [_make_candidate("proj")]
        result = _run_prompt(candidates, "n\n")
        assert result == []

    def test_y_answer_with_no_nested_git_and_init_yes(self, tmp_path: Path) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        cand = _make_candidate("proj", str(folder), nested=())
        with patch(
            "pipeline.project.project_discovery_prompt._run_git_init",
            return_value=True,
        ):
            result = _run_prompt([cand], "y\ny\n")
        assert len(result) == 1
        assert result[0].name == "proj"
        assert result[0].git_dir == ""

    def test_y_answer_with_no_nested_git_and_init_no(self, tmp_path: Path) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        cand = _make_candidate("proj", str(folder), nested=())
        with patch(
            "pipeline.project.project_discovery_prompt._run_git_init",
        ) as mock_init:
            result = _run_prompt([cand], "y\nn\n")
        mock_init.assert_not_called()
        assert len(result) == 1
        assert result[0].git_dir == ""

    def test_empty_answer_defaults_to_no(self) -> None:
        candidates = [_make_candidate()]
        result = _run_prompt(candidates, "\n")
        assert result == []

    def test_eof_on_main_question_skips(self) -> None:
        stdin = io.StringIO("")
        stdout = io.StringIO()
        result = prompt_for_extra_projects(
            [_make_candidate()], stdin=stdin, stdout=stdout
        )
        assert result == []


class TestOneNestedGit:
    def test_yes_registers_with_git_dir(self) -> None:
        cand = _make_candidate("mono", nested=("SubProject",))
        result = _run_prompt([cand], "y\ny\n")
        assert len(result) == 1
        assert result[0].git_dir == "SubProject"

    def test_no_skips(self) -> None:
        cand = _make_candidate("mono", nested=("SubProject",))
        result = _run_prompt([cand], "y\nn\n")
        assert result == []


class TestMultiNestedGit:
    def test_picks_first_by_default(self) -> None:
        cand = _make_candidate(
            "mono", nested=("shallow/src", "deep/a/b/src")
        )
        result = _run_prompt([cand], "y\n\n")  # blank = default = 1 = first
        assert len(result) == 1
        assert result[0].git_dir == "shallow/src"

    def test_picks_second_by_explicit_choice(self) -> None:
        cand = _make_candidate(
            "mono", nested=("shallow/src", "deep/a/b/src")
        )
        result = _run_prompt([cand], "y\n2\n")
        assert len(result) == 1
        assert result[0].git_dir == "deep/a/b/src"

    def test_exit_idx_skips(self) -> None:
        cand = _make_candidate("mono", nested=("a/src", "b/src"))
        result = _run_prompt([cand], "y\n3\n")  # exit idx = len+1 = 3
        assert result == []


class TestNoGitInit:
    def test_git_init_not_run_when_user_declines(self, tmp_path: Path) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        cand = _make_candidate("proj", str(folder), nested=())
        with patch(
            "pipeline.project.project_discovery_prompt._run_git_init",
        ) as mock_init:
            _run_prompt([cand], "y\nn\n")
        mock_init.assert_not_called()

    def test_git_init_creates_repo(self, tmp_path: Path) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        cand = _make_candidate("proj", str(folder), nested=())
        # Let the real git init run.
        result = _run_prompt([cand], "y\ny\n")
        assert (folder / ".git").exists()
        assert result[0].git_dir == ""

    def test_no_git_init_when_nested_git_found(self, tmp_path: Path) -> None:
        folder = tmp_path / "mono"
        folder.mkdir()
        cand = _make_candidate("mono", str(folder), nested=("src",))
        with patch(
            "pipeline.project.project_discovery_prompt._run_git_init",
        ) as mock_init:
            _run_prompt([cand], "y\ny\n")
        mock_init.assert_not_called()


class TestNonInteractiveNoGitInit:
    """git init must never run when prompt_for_extra_projects is not called
    (i.e. caller gates on TTY / --no-interactive)."""

    def test_calling_with_empty_candidates_returns_empty(self) -> None:
        result = _run_prompt([], "")
        assert result == []
