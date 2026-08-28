"""Contract tests for explicit project-plugin materialisation."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pipeline.plugins import PLUGIN_RELATIVE_PATH, describe_plugin, load_plugin
from sdk.workspace_project_plugin import materialize_project_plugins
from sdk.workspace_scaffold import render_plugin_template, scaffold_workspace_extensions


def _python_project(root: Path, name: str = "python-project") -> Path:
    project = root / name
    package = project / "demo_pkg"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return project


def _markerless_project(root: Path, name: str = "markerless") -> Path:
    project = root / name
    project.mkdir()
    (project / "README.md").write_text("project\n", encoding="utf-8")
    return project


def test_workspace_scaffold_uses_empty_shared_template(tmp_path: Path) -> None:
    result = scaffold_workspace_extensions(tmp_path / "workspace", dry_run=False)
    plugin_path = Path(result.extension_points[0])

    assert plugin_path.read_text(encoding="utf-8") == render_plugin_template()
    module = ast.parse(plugin_path.read_text(encoding="utf-8"))
    assignment = next(
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PLUGIN" for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Dict)
    assert assignment.value.keys == []
    plugin = load_plugin(str(tmp_path / "workspace"))
    assert plugin.loaded_plugin_path == str(plugin_path)
    assert plugin.verification == {}


def test_python_candidate_materializes_as_loadable_plugin(tmp_path: Path) -> None:
    project = _python_project(tmp_path)

    (outcome,) = materialize_project_plugins([project])

    plugin_path = project / PLUGIN_RELATIVE_PATH
    assert outcome.status == "created"
    ast.parse(plugin_path.read_text(encoding="utf-8"))
    plugin = load_plugin(str(project))
    assert plugin.loaded_plugin_path == str(plugin_path)
    assert {"import": "demo_pkg", "path_under": "{checkout}"} in (
        plugin.verification_envs["py"]["assertions"]
    )
    assert plugin.verification["commands"]["lint"]["run"] == "ruff check ."
    assert plugin.verification["commands"]["test"]["run"] == "pytest -q"
    assert "generic mode" not in describe_plugin(plugin)


def test_markerless_project_materializes_a_loadable_plugin(tmp_path: Path) -> None:
    project = _markerless_project(tmp_path)

    (outcome,) = materialize_project_plugins([project])

    assert outcome.status == "created"
    plugin = load_plugin(str(project))
    assert plugin.loaded_plugin_path.endswith(PLUGIN_RELATIVE_PATH)
    assert plugin.verification["commands"] == {}
    assert "generic mode" not in describe_plugin(plugin)


def test_multiple_registered_projects_are_materialized(tmp_path: Path) -> None:
    python_project = _python_project(tmp_path)
    markerless_project = _markerless_project(tmp_path)

    outcomes = materialize_project_plugins([python_project, markerless_project])

    assert [outcome.status for outcome in outcomes] == ["created", "created"]
    assert all(Path(outcome.destination).is_file() for outcome in outcomes)


def test_repeat_and_existing_file_preserve_plugin_bytes(tmp_path: Path) -> None:
    project = _python_project(tmp_path)
    (first,) = materialize_project_plugins([project])
    plugin_path = Path(first.destination)
    before = plugin_path.read_bytes()

    (repeat,) = materialize_project_plugins([project])

    assert repeat.status == "skipped"
    assert plugin_path.read_bytes() == before


@pytest.mark.parametrize("entry", ["file", "directory", "symlink"])
def test_existing_destination_entries_are_never_overwritten(
    tmp_path: Path, entry: str,
) -> None:
    project = _markerless_project(tmp_path, name=entry)
    destination = project / PLUGIN_RELATIVE_PATH
    destination.parent.mkdir(parents=True)
    if entry == "file":
        destination.write_text("PLUGIN = {'name': 'keep'}\n", encoding="utf-8")
        before = destination.read_bytes()
    elif entry == "directory":
        destination.mkdir()
        before = None
    else:
        destination.symlink_to(project / "missing-plugin-target")
        before = None

    (outcome,) = materialize_project_plugins([project])

    assert outcome.status == "skipped"
    if entry == "file":
        assert destination.read_bytes() == before
    elif entry == "directory":
        assert destination.is_dir()
    else:
        assert destination.is_symlink()
