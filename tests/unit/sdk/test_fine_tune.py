"""T4 — ``sdk.fine_tune.fine_tune_project`` candidate-contract inspection.

The load-bearing guarantee is no-write: inspecting a project must not create
or modify a single file. The proof (F3) is a content fingerprint of the whole
tree before and after — relative path + size + sha256 — compared for full
equality, which catches both new and mutated files (including a pre-existing
plugin.py / pyproject.toml).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.infra.platform import venv_python_subpath
from sdk.fine_tune import FineTuneResult, fine_tune_project


def _fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """Map each file's project-relative path → (size, sha256)."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        rel = str(path.relative_to(root))
        out[rel] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def _python_project(root: Path, *, pkg: str = "proj_pkg", venv: bool = False) -> Path:
    project = root / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\n", encoding="utf-8",
    )
    package = project / pkg
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    # A pre-existing plugin.py must also remain untouched.
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("PLUGIN = {}\n", encoding="utf-8")
    if venv:
        venv_python = project / venv_python_subpath()
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    return project


class TestCandidateContract:
    def test_python_project_yields_envs_and_commands(self, tmp_path: Path) -> None:
        project = _python_project(tmp_path)
        result = fine_tune_project(str(project), dry_run=True)

        assert isinstance(result, FineTuneResult)
        assert result.markers == ["pyproject.toml"]
        envs = result.candidate["verification_envs"]
        assert "py" in envs
        # import assertion targets the detected local package.
        assertions = envs["py"]["assertions"]
        assert {"import": "proj_pkg", "path_under": "{checkout}"} in assertions
        commands = result.candidate["verification"]["commands"]
        assert "lint" in commands and "test" in commands
        assert result.candidate["verification"]["default_env"] == "py"
        assert result.candidate["work_mode"] == "pro"

    def test_venv_python_is_surfaced(self, tmp_path: Path) -> None:
        project = _python_project(tmp_path, venv=True)
        result = fine_tune_project(str(project), dry_run=True)
        assert result.candidate["verification_envs"]["py"]["python"] == (
            f"{{checkout}}/{venv_python_subpath()}"
        )

    def test_node_project_detected(self, tmp_path: Path) -> None:
        project = tmp_path / "n"
        project.mkdir()
        (project / "package.json").write_text("{}\n", encoding="utf-8")
        result = fine_tune_project(str(project), dry_run=True)
        assert "node" in result.candidate["verification_envs"]
        assert result.candidate["verification"]["default_env"] == "node"

    def test_dotnet_solution_detected_with_libs_bootstrap_hint(
        self,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "bot"
        project.mkdir()
        (project / "Bot.sln").write_text("\n", encoding="utf-8")
        libs = project / "libs"
        libs.mkdir()
        (libs / "Vendor.dll").write_text("dll\n", encoding="utf-8")

        result = fine_tune_project(str(project), dry_run=True)

        assert result.markers == ["*.sln"]
        assert "dotnet" in result.candidate["verification_envs"]
        assertions = result.candidate["verification_envs"]["dotnet"]["assertions"]
        assert {"command_exists": "dotnet"} in assertions
        assert {"path_exists": "libs"} in assertions
        commands = result.candidate["verification"]["commands"]
        assert commands["dotnet_build"]["run"] == "dotnet build"
        assert commands["worktree_bootstrap_hint"]["worktree_bootstrap"] == [
            {"copy": "libs"},
        ]

    def test_workspace_root_suggests_child_project_roots(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        py = workspace / "orcho-core"
        py.mkdir()
        (py / "pyproject.toml").write_text("[project]\nname='core'\n", encoding="utf-8")
        dotnet = workspace / "atas" / "bot_1"
        dotnet.mkdir(parents=True)
        (dotnet / "Bot.sln").write_text("\n", encoding="utf-8")
        nested = dotnet / "Core.Tests"
        nested.mkdir()
        (nested / "Core.Tests.csproj").write_text("<Project />\n", encoding="utf-8")

        result = fine_tune_project(str(workspace), dry_run=True)

        assert result.markers == []
        assert result.candidate["verification_envs"] == {}
        assert result.suggested_projects == [str(py), str(dotnet)]

    def test_no_markers_yields_empty_candidate(self, tmp_path: Path) -> None:
        project = tmp_path / "bare"
        project.mkdir()
        (project / "README.md").write_text("hi\n", encoding="utf-8")
        result = fine_tune_project(str(project), dry_run=True)
        assert result.markers == []
        assert result.candidate["verification_envs"] == {}
        assert result.candidate["verification"]["default_env"] == ""
        assert result.suggested_projects == []


class TestNoWrite:
    def test_dry_run_writes_nothing_fingerprint(self, tmp_path: Path) -> None:
        project = _python_project(tmp_path, venv=True)

        before = _fingerprint(project)
        result = fine_tune_project(str(project), dry_run=True)
        after = _fingerprint(project)

        assert result.wrote is False
        # Full equality catches both new and modified files.
        assert before == after
        # Non-empty candidate so the no-write proof is meaningful.
        assert result.candidate["verification_envs"]

    def test_non_dry_run_also_writes_nothing(self, tmp_path: Path) -> None:
        project = _python_project(tmp_path)

        before = _fingerprint(project)
        result = fine_tune_project(str(project), dry_run=False)
        after = _fingerprint(project)

        assert result.wrote is False
        assert before == after
