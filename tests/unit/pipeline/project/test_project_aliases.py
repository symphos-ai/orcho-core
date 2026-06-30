"""Unit tests for pipeline.project.project_aliases."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.project.project_aliases import (
    load_workspace_project_aliases,
    load_workspace_project_git_dir,
)


def _write_config(workspace_dir: Path, projects: dict) -> Path:
    config_dir = workspace_dir / ".orcho"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.local.json"
    config_path.write_text(
        json.dumps({"projects": projects}),
        encoding="utf-8",
    )
    return config_path


class TestLoadWorkspaceProjectAliases:
    def test_string_entries_parsed(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write_config(ws, {"proj-a": str(tmp_path / "proj-a")})
        aliases = load_workspace_project_aliases(workspace=ws)
        assert "proj-a" in aliases
        assert aliases["proj-a"] == (tmp_path / "proj-a").resolve()

    def test_object_entries_parsed(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj-b"
        ws = tmp_path / "ws"
        _write_config(ws, {
            "proj-b": {"path": str(proj), "git_dir": "src"},
        })
        aliases = load_workspace_project_aliases(workspace=ws)
        assert "proj-b" in aliases
        assert aliases["proj-b"] == proj.resolve()

    def test_mixed_string_and_object(self, tmp_path: Path) -> None:
        proj_a = tmp_path / "a"
        proj_b = tmp_path / "b"
        ws = tmp_path / "ws"
        _write_config(ws, {
            "a": str(proj_a),
            "b": {"path": str(proj_b), "git_dir": "nested"},
        })
        aliases = load_workspace_project_aliases(workspace=ws)
        assert set(aliases) == {"a", "b"}

    def test_object_with_empty_git_dir(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        ws = tmp_path / "ws"
        _write_config(ws, {"proj": {"path": str(proj), "git_dir": ""}})
        aliases = load_workspace_project_aliases(workspace=ws)
        assert "proj" in aliases

    def test_invalid_entries_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write_config(ws, {
            "": "/some/path",          # empty alias
            "valid": str(tmp_path / "v"),
            "bad-obj": {"not_path": "x"},  # object without "path"
        })
        aliases = load_workspace_project_aliases(workspace=ws)
        assert set(aliases) == {"valid"}

    def test_missing_config_returns_empty(self, tmp_path: Path) -> None:
        ws = tmp_path / "no-config"
        ws.mkdir()
        aliases = load_workspace_project_aliases(workspace=ws)
        assert aliases == {}


class TestLoadWorkspaceProjectGitDir:
    def test_returns_git_dir_for_object_entry(self, tmp_path: Path) -> None:
        proj = tmp_path / "mono"
        ws = tmp_path / "ws"
        _write_config(ws, {"mono": {"path": str(proj), "git_dir": "SubProject"}})
        result = load_workspace_project_git_dir(proj, workspace=ws)
        assert result == "SubProject"

    def test_returns_empty_for_string_entry(self, tmp_path: Path) -> None:
        proj = tmp_path / "plain"
        ws = tmp_path / "ws"
        _write_config(ws, {"plain": str(proj)})
        result = load_workspace_project_git_dir(proj, workspace=ws)
        assert result == ""

    def test_returns_empty_when_project_not_registered(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".orcho").mkdir()
        (ws / ".orcho" / "config.local.json").write_text(
            json.dumps({"projects": {}}), encoding="utf-8",
        )
        result = load_workspace_project_git_dir(tmp_path / "unknown", workspace=ws)
        assert result == ""

    def test_returns_empty_for_object_with_empty_git_dir(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        ws = tmp_path / "ws"
        _write_config(ws, {"proj": {"path": str(proj), "git_dir": ""}})
        result = load_workspace_project_git_dir(proj, workspace=ws)
        assert result == ""

    def test_resolves_symlinks_for_matching(self, tmp_path: Path) -> None:
        proj = tmp_path / "actual"
        proj.mkdir()
        ws = tmp_path / "ws"
        _write_config(ws, {"actual": str(proj)})
        result = load_workspace_project_git_dir(proj, workspace=ws)
        assert result == ""
