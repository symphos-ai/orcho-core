"""T4 — ``sdk.fine_tune.fine_tune_project`` candidate-contract inspection.

The load-bearing guarantee is no-write: inspecting a project must not create
or modify a single file. The proof (F3) is a content fingerprint of the whole
tree before and after — relative path + size + sha256 — compared for full
equality, which catches both new and mutated files (including a pre-existing
plugin.py / pyproject.toml).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.infra.platform import venv_python_subpath
from pipeline.plugins import PLUGIN_RELATIVE_PATH, load_plugin
from pipeline.verification_contract import VerificationContract
from sdk.fine_tune import FineTuneResult, fine_tune_project
from sdk.fine_tune_probes import EnvCandidate, register_marker_probe
from sdk.workspace_scaffold import render_plugin_template


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


def _node_project(
    root: Path,
    *,
    scripts: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    project = root / "node-proj"
    project.mkdir()
    manifest: dict[str, Any] = {"name": "node-proj", **(extra or {})}
    if scripts is not None:
        manifest["scripts"] = scripts
    (project / "package.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )
    return project


def _git_project(root: Path, relative: str) -> tuple[Path, Path]:
    """Create a Git worktree and return it with its nested project directory."""
    repository = root / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", str(repository)], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    project = repository / relative
    project.mkdir(parents=True, exist_ok=True)
    return repository, project


def _git_add(repository: Path, path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repository), "add", "--", str(path.relative_to(repository))],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _assert_round_trip_candidate(project: Path, candidate: dict[str, Any]) -> None:
    plugin_path = project / PLUGIN_RELATIVE_PATH
    plugin_path.parent.mkdir(parents=True)
    plugin_path.write_text(render_plugin_template(candidate), encoding="utf-8")

    plugin = load_plugin(str(project))
    assert plugin.loaded_plugin_path == str(plugin_path)
    assert VerificationContract.from_plugin(plugin) is not None


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

    def test_node_scripts_drive_proposed_commands(self, tmp_path: Path) -> None:
        project = _node_project(tmp_path, scripts={
            "test": "vitest run",
            "lint": "eslint .",
            "typecheck": "tsc --noEmit",
        })
        result = fine_tune_project(str(project), dry_run=True)
        commands = result.candidate["verification"]["commands"]
        assert commands["node_test"] == {"run": "npm test", "env": "node"}
        assert commands["node_lint"] == {"run": "npm run lint", "env": "node"}
        assert commands["node_typecheck"] == {
            "run": "npm run typecheck", "env": "node",
        }

    def test_node_absent_scripts_are_not_proposed(self, tmp_path: Path) -> None:
        # Only a lint script: no dead-on-arrival npm test / npm run typecheck.
        project = _node_project(tmp_path, scripts={"lint": "eslint ."})
        result = fine_tune_project(str(project), dry_run=True)
        commands = result.candidate["verification"]["commands"]
        assert set(commands) == {"node_lint"}
        assert "node" in result.candidate["verification_envs"]

    def test_node_typescript_devdep_falls_back_to_npx_tsc(
        self,
        tmp_path: Path,
    ) -> None:
        project = _node_project(
            tmp_path,
            scripts={"test": "vitest run"},
            extra={"devDependencies": {"typescript": "^5.4.0"}},
        )
        result = fine_tune_project(str(project), dry_run=True)
        commands = result.candidate["verification"]["commands"]
        assert commands["node_typecheck"] == {
            "run": "npx tsc --noEmit", "env": "node",
        }

    def test_node_test_colon_scripts_surface_as_alternates(
        self,
        tmp_path: Path,
    ) -> None:
        project = _node_project(tmp_path, scripts={
            "test": "vitest run",
            "test:unit": "vitest run --project unit",
            "test:e2e": "playwright test",
        })
        result = fine_tune_project(str(project), dry_run=True)
        assert result.candidate["suggested_alternates"] == [
            {"name": "test:e2e", "run": "npm run test:e2e", "env": "node"},
            {"name": "test:unit", "run": "npm run test:unit", "env": "node"},
        ]

    def test_node_unreadable_manifest_falls_back_to_npm_test(
        self,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "broken"
        project.mkdir()
        (project / "package.json").write_text("{not json", encoding="utf-8")
        result = fine_tune_project(str(project), dry_run=True)
        commands = result.candidate["verification"]["commands"]
        assert set(commands) == {"node_test"}
        assert commands["node_test"]["run"] == "npm test"

    def test_registered_marker_probe_extends_detection(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import sdk.fine_tune_probes as probes

        def _probe_just(root: Path) -> EnvCandidate:
            return EnvCandidate(
                env="just",
                spec={"assertions": [{"command_exists": "just"}]},
                commands={"just_test": {"run": "just test", "env": "just"}},
            )

        monkeypatch.setitem(probes._MARKER_PROBES, "justfile", _probe_just)
        register_marker_probe("justfile", _probe_just)

        project = tmp_path / "j"
        project.mkdir()
        (project / "justfile").write_text("test:\n", encoding="utf-8")
        result = fine_tune_project(str(project), dry_run=True)
        assert result.markers == ["justfile"]
        assert "just" in result.candidate["verification_envs"]
        assert result.candidate["verification"]["commands"]["just_test"] == {
            "run": "just test", "env": "just",
        }

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

    def test_nested_node_sets_checkout_cwd_and_bootstraps_untracked_modules(
        self,
        tmp_path: Path,
    ) -> None:
        repository, project = _git_project(tmp_path, "sub/web")
        (project / "package.json").write_text(
            json.dumps({"name": "web", "scripts": {"test": "vitest run"}}),
            encoding="utf-8",
        )
        (project / "package-lock.json").write_text("{}\n", encoding="utf-8")
        # An ignored/untracked install directory is not evidence a fresh
        # worktree has dependencies available.
        (project / "node_modules").mkdir()
        (project / "node_modules" / "local.js").write_text("// local\n", encoding="utf-8")
        _git_add(repository, project / "package.json")
        _git_add(repository, project / "package-lock.json")

        result = fine_tune_project(str(project), dry_run=True)

        assert result.candidate["verification_envs"]["node"]["cwd"] == (
            "{checkout}/sub/web"
        )
        assert result.candidate["worktree_bootstrap"] == [
            {"run": ["npm", "ci"], "cwd": "sub/web"},
        ]
        _assert_round_trip_candidate(project, result.candidate)

    def test_toplevel_python_has_no_cwd_or_bootstrap(self, tmp_path: Path) -> None:
        repository, project = _git_project(tmp_path, ".")
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'top'\n", encoding="utf-8",
        )
        (project / "poetry.lock").write_text("# lock\n", encoding="utf-8")
        _git_add(repository, project / "pyproject.toml")
        _git_add(repository, project / "poetry.lock")

        result = fine_tune_project(str(project), dry_run=True)

        assert "cwd" not in result.candidate["verification_envs"]["py"]
        assert "worktree_bootstrap" not in result.candidate
        _assert_round_trip_candidate(project, result.candidate)

    @pytest.mark.parametrize(
        ("lockfile", "artifact", "expected"),
        [
            ("package-lock.json", "node_modules", ["npm", "ci"]),
            ("poetry.lock", ".venv", ["poetry", "install"]),
            ("composer.lock", "vendor", ["composer", "install", "--no-interaction"]),
            ("Cargo.lock", "target", None),
        ],
    )
    def test_nested_lockfiles_propose_only_missing_bootstrap(
        self,
        tmp_path: Path,
        lockfile: str,
        artifact: str,
        expected: list[str] | None,
    ) -> None:
        repository, project = _git_project(tmp_path, "sub/project")
        (project / lockfile).write_text("lock\n", encoding="utf-8")
        if expected is not None:
            untracked = project / artifact / "local-state"
            untracked.parent.mkdir(parents=True)
            untracked.write_text("untracked\n", encoding="utf-8")
        _git_add(repository, project / lockfile)

        result = fine_tune_project(str(project), dry_run=True)

        if expected is None:
            assert "worktree_bootstrap" not in result.candidate
        else:
            assert result.candidate["worktree_bootstrap"] == [
                {"run": expected, "cwd": "sub/project"},
            ]

    @pytest.mark.parametrize(
        ("lockfile", "artifact"),
        [
            ("package-lock.json", "node_modules"),
            ("poetry.lock", ".venv"),
            ("composer.lock", "vendor"),
        ],
    )
    def test_tracked_install_artifacts_suppress_bootstrap(
        self,
        tmp_path: Path,
        lockfile: str,
        artifact: str,
    ) -> None:
        repository, project = _git_project(tmp_path, "sub/project")
        (project / lockfile).write_text("lock\n", encoding="utf-8")
        tracked = project / artifact / ".keep"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("tracked\n", encoding="utf-8")
        _git_add(repository, project / lockfile)
        _git_add(repository, tracked)

        result = fine_tune_project(str(project), dry_run=True)

        assert "worktree_bootstrap" not in result.candidate

    def test_schedule_uses_explicit_fast_and_unknown_costs_in_command_order(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import sdk.fine_tune_probes as probes

        def _probe_costs(root: Path) -> EnvCandidate:
            return EnvCandidate(
                env="costs",
                spec={"assertions": [{"command_exists": "python"}]},
                commands={
                    "fast_check": {"run": "python -m compileall .", "env": "costs", "cost": "fast"},
                    "unknown_check": {"run": "python -m pytest", "env": "costs"},
                },
            )

        monkeypatch.setitem(probes._MARKER_PROBES, "cost.marker", _probe_costs)
        repository, project = _git_project(tmp_path, "sub/web")
        (project / "cost.marker").write_text("\n", encoding="utf-8")
        _git_add(repository, project / "cost.marker")

        result = fine_tune_project(str(project), dry_run=True)

        verification = result.candidate["verification"]
        assert verification["required"] == ["fast_check", "unknown_check"]
        assert verification["schedule"] == [
            {
                "after_phase": "implement",
                "policy": "warn",
                "commands": ["fast_check"],
            },
            {
                "before_delivery": True,
                "policy": "warn",
                "commands": ["unknown_check"],
            },
        ]
        assert "cost" not in verification["commands"]["unknown_check"]

    def test_non_git_project_is_its_own_root(self, tmp_path: Path) -> None:
        project = _node_project(tmp_path, scripts={"test": "vitest run"})
        (project / "package-lock.json").write_text("{}\n", encoding="utf-8")

        result = fine_tune_project(str(project), dry_run=True)

        assert "cwd" not in result.candidate["verification_envs"]["node"]
        assert "worktree_bootstrap" not in result.candidate


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
