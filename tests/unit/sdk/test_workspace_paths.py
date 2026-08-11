from __future__ import annotations

import json
from pathlib import Path

from sdk.workspace_paths import (
    infer_workspace_from_project,
    managed_workspace_dir,
    project_repo_marker,
)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def test_managed_workspace_is_external_and_deterministic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _repo(tmp_path / "projects" / "api")
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    first = managed_workspace_dir(project)
    second = managed_workspace_dir(project / ".")

    assert first == second
    assert first.parent.parent == data_home / "orcho" / "workspaces"
    assert first.name == "workspace-orchestrator"
    assert project not in first.parents


def test_same_basename_projects_have_distinct_managed_workspaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    first = _repo(tmp_path / "one" / "api")
    second = _repo(tmp_path / "two" / "api")

    assert managed_workspace_dir(first) != managed_workspace_dir(second)


def test_infer_prefers_existing_group_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    project = _repo(tmp_path / "group" / "api")
    sibling = tmp_path / "group" / "workspace-orchestrator"
    sibling.mkdir()
    config = sibling / ".orcho" / "config.local.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"projects": {"api": str(project)}}),
        encoding="utf-8",
    )
    managed_workspace_dir(project).mkdir(parents=True)

    assert infer_workspace_from_project(project) == sibling


def test_infer_ignores_unregistered_group_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    project = _repo(tmp_path / "group" / "api")
    sibling = tmp_path / "group" / "workspace-orchestrator"
    config = sibling / ".orcho" / "config.local.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"projects": {"web": str(tmp_path / "group" / "web")}}),
        encoding="utf-8",
    )
    managed = managed_workspace_dir(project)
    managed.mkdir(parents=True)

    assert infer_workspace_from_project(project) == managed


def test_infer_falls_back_to_existing_managed_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    project = _repo(tmp_path / "projects" / "api")
    managed = managed_workspace_dir(project)
    managed.mkdir(parents=True)

    assert infer_workspace_from_project(project) == managed


def test_infer_does_not_create_managed_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    project = _repo(tmp_path / "projects" / "api")
    managed = managed_workspace_dir(project)

    assert infer_workspace_from_project(project) is None
    assert not managed.exists()


def test_project_repo_marker_is_root_scoped(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    nested = tmp_path / "parent"
    (nested / "child" / ".git").mkdir(parents=True)

    assert project_repo_marker(project) == "pyproject.toml"
    assert project_repo_marker(nested) is None
