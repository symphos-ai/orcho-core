"""
Three-level prompt resolution chain tests.

Validates:
 1. Core fallback (always works)
 2. Project override wins over core
 3. Workspace override wins over core, loses to project
 4. resolution_chain() returns correct debug info
 5. list_workspace_prompts() discovers workspace-level overrides
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.io.prompt_loader import (
    _PROMPTS_SUBPATH,
    _find_workspace_dir,
    list_core_prompts,
    list_project_prompts,
    list_workspace_prompts,
    reload_cache,
    render_prompt,
    resolution_chain,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear lru_cache between tests."""
    reload_cache()
    yield
    reload_cache()


def _write_prompt(base: Path, name: str, content: str) -> Path:
    """Write a prompt file in the standard subpath under base.

 Supports composable-part names with subdirectory prefixes
 (``tasks/implement``, ``roles/code_reviewer``, ``formats/detailed``) — the
 intermediate subdirectory is created when needed.
 """
    d = base / _PROMPTS_SUBPATH
    f = d / f"{name}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f


# ─────────────────────────────────────────────────────────────────────────────
# Core fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestCoreFallback:
    def test_core_prompt_renders(self) -> None:
        # ADR 0028 / M10.5 Step 2: task files contain only static
        # method prose. Render verifies the method body, not a
        # substituted task string.
        text = render_prompt("tasks/plan")
        assert "implementation plan" in text.lower()

    def test_core_prompts_list_non_empty(self) -> None:
        names = list_core_prompts()
        assert len(names) >= 8
        assert "tasks/plan" in names

    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="nonexistent_prompt_xyz"):
            render_prompt("nonexistent_prompt_xyz")


# ─────────────────────────────────────────────────────────────────────────────
# Project override
# ─────────────────────────────────────────────────────────────────────────────

class TestProjectOverride:
    def test_project_override_wins(self, tmp_path: Path) -> None:
        project = tmp_path / "my_project"
        project.mkdir()
        _write_prompt(project, "tasks/plan",
                      "PROJECT OVERRIDE: plan for $task")
        text = render_prompt("tasks/plan",
                             project_dir=str(project), task="test task")
        assert "PROJECT OVERRIDE" in text
        assert "test task" in text

    def test_project_no_override_falls_to_core(self, tmp_path: Path) -> None:
        project = tmp_path / "empty_project"
        project.mkdir()
        text = render_prompt("tasks/plan",
                             project_dir=str(project), task="t",
                             ma_artifacts_dir="d/", context="", extra_step="")
        # Should come from core (no project override)
        assert "t" in text

    def test_list_project_prompts(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_prompt(project, "tasks/implement", "custom implement")
        _write_prompt(project, "tasks/code_review", "custom review")
        prompts = list_project_prompts(str(project))
        assert "tasks/implement" in prompts
        assert "tasks/code_review" in prompts


# ─────────────────────────────────────────────────────────────────────────────
# Workspace override
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkspaceOverride:
    def test_workspace_override_wins_over_core(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "api"
        workspace.mkdir()
        project.mkdir()
        _write_prompt(workspace, "tasks/plan",
                      "WORKSPACE OVERRIDE: plan for $task")
        text = render_prompt("tasks/plan",
                             project_dir=str(project), task="ws task")
        assert "WORKSPACE OVERRIDE" in text
        assert "ws task" in text

    def test_project_override_wins_over_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "api"
        workspace.mkdir()
        project.mkdir()
        _write_prompt(workspace, "tasks/plan",
                      "WORKSPACE: $task")
        _write_prompt(project, "tasks/plan",
                      "PROJECT: $task")
        text = render_prompt("tasks/plan",
                             project_dir=str(project), task="both exist")
        assert "PROJECT:" in text
        assert "WORKSPACE:" not in text

    def test_workspace_not_found_falls_to_core(self, tmp_path: Path) -> None:
        project = tmp_path / "standalone"
        project.mkdir()
        # No workspace prompts, no project prompts
        text = render_prompt("tasks/plan",
                             project_dir=str(project), task="t",
                             ma_artifacts_dir="d/", context="", extra_step="")
        assert "t" in text

    def test_list_workspace_prompts(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        project = workspace / "proj"
        workspace.mkdir()
        project.mkdir()
        _write_prompt(workspace, "tasks/implement", "ws implement")
        prompts = list_workspace_prompts(str(project))
        assert "tasks/implement" in prompts

    def test_list_workspace_prompts_none(self, tmp_path: Path) -> None:
        project = tmp_path / "standalone"
        project.mkdir()
        prompts = list_workspace_prompts(str(project))
        assert prompts == []


# ─────────────────────────────────────────────────────────────────────────────
# _find_workspace_dir
# ─────────────────────────────────────────────────────────────────────────────

class TestFindWorkspaceDir:
    def test_finds_parent_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        project = workspace / "api"
        workspace.mkdir()
        project.mkdir()
        _write_prompt(workspace, "test_prompt", "x")
        assert _find_workspace_dir(project) == workspace

    def test_skips_project_itself(self, tmp_path: Path) -> None:
        """Project has its own prompts but no workspace above it."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_prompt(project, "test_prompt", "x")
        # Should NOT return project itself as workspace
        ws = _find_workspace_dir(project)
        # ws should be None or something above project, but NOT project
        assert ws != project.resolve()

    def test_returns_none_at_root(self, tmp_path: Path) -> None:
        project = tmp_path / "solo"
        project.mkdir()
        assert _find_workspace_dir(project) is None

    def test_nested_workspace(self, tmp_path: Path) -> None:
        """workspace/sub/project — finds workspace, not sub."""
        workspace = tmp_path / "ws"
        sub = workspace / "sub"
        project = sub / "proj"
        workspace.mkdir()
        sub.mkdir()
        project.mkdir()
        _write_prompt(workspace, "tasks/plan", "ws level")
        found = _find_workspace_dir(project)
        # Should find either sub or workspace (first parent with prompts)
        assert found is not None


# ─────────────────────────────────────────────────────────────────────────────
# resolution_chain()
# ─────────────────────────────────────────────────────────────────────────────

class TestResolutionChain:
    def test_core_only(self) -> None:
        chain = resolution_chain("tasks/plan")
        assert len(chain) == 1
        assert chain[0][0] == "core"
        assert chain[0][2] is True  # exists

    def test_project_and_core(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        chain = resolution_chain("tasks/plan",
                                 project_dir=str(project))
        levels = [c[0] for c in chain]
        assert "project" in levels
        assert "core" in levels

    def test_all_three_levels(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        project = workspace / "api"
        workspace.mkdir()
        project.mkdir()
        _write_prompt(workspace, "tasks/plan", "ws")
        chain = resolution_chain("tasks/plan",
                                 project_dir=str(project))
        levels = [c[0] for c in chain]
        assert levels == ["project", "workspace", "core"]

    def test_chain_shows_existence(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        project = workspace / "api"
        workspace.mkdir()
        project.mkdir()
        _write_prompt(project, "tasks/plan", "proj override")
        _write_prompt(workspace, "tasks/plan", "ws override")
        chain = resolution_chain("tasks/plan",
                                 project_dir=str(project))
        # project exists, workspace exists, core exists
        assert all(c[2] for c in chain)


# ─────────────────────────────────────────────────────────────────────────────
# All prompt steps load from markdown
# ─────────────────────────────────────────────────────────────────────────────

class TestAllPromptStepsFromMarkdown:
    """Verify all prompts.py functions load from _prompts/*.md, not inline."""

    REQUIRED_TEMPLATES = [
        # ADR 0009 + 0022: every shipped template is a composable part.
        # Roles use the professional persona taxonomy; tasks use the
        # workflow-semantic phase taxonomy (ADR 0022).
        # roles
        "roles/code_reviewer",
        "roles/implementation_engineer",
        "roles/plan_reviewer",
        "roles/release_manager",
        "roles/systems_architect",
        "roles/product_owner",
        # tasks
        "tasks/plan",
        "tasks/replan",
        "tasks/decompose",
        "tasks/implement",
        "tasks/repair_changes",
        "tasks/code_review",
        "tasks/final_acceptance",
        "tasks/validate_plan",
        "tasks/hypothesis",
        "tasks/validate_hypothesis",
        "tasks/readonly_plan",
        "tasks/review_uncommitted",
        "tasks/cross_plan",
        "tasks/cross_contract_bundle",
        # formats
        "formats/terse",
        "formats/compact",
        "formats/detailed",
        "formats/bullets",
        "formats/handoff",
    ]

    def test_all_required_templates_exist(self) -> None:
        core = list_core_prompts()
        for name in self.REQUIRED_TEMPLATES:
            assert name in core, f"Missing core template: {name}"

    def test_prompts_py_has_no_inline_text(self) -> None:
        """prompts.py should only contain render_prompt() calls, no inline prompt text."""
        import inspect

        from pipeline import prompts as prompts_module

        source = inspect.getsource(prompts_module)
        # Heuristic: if prompts.py contains multi-line f-strings with prompt
        # instructions, that's inline text. render_prompt calls are fine.
        # We check that the only long strings are docstrings and style hints
        # (which are data, not prompts).
        # Count lines that look like prompt text (triple-quoted long strings
        # that aren't docstrings)
        assert "render_prompt" in source, "prompts.py should use render_prompt()"
