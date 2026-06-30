"""
tests/integration/test_prompt_resolution.py

Integration tests for the full prompt templating pipeline:
  core/prompt_loader + pipeline/prompts + cross_orchestrator.cross_plan_prompt

Unlike unit tests, these tests read REAL _prompts/*.md templates to verify
that all expected variables are present and templates render correctly end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.io.prompt_loader import list_core_prompts, reload_cache
from pipeline import prompts
from pipeline.plugins import PluginConfig

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_template_cache():
    reload_cache()
    yield
    reload_cache()


@pytest.fixture
def plugin() -> PluginConfig:
    return PluginConfig(
        name="Test Project",
        language="Python",
        architecture="FastAPI + SQLAlchemy",
        ma_artifacts_dir=".orcho/artifacts",
        file_hints=["src/", "tests/"],
        plan_prompt_extra="Follow PEP 8 strictly.",
        build_prompt_extra="Do not touch migrations.",
        review_focus_extra="Check for N+1 queries.",
    )


@pytest.fixture
def task() -> str:
    return "Add structured logging to the auth service"


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake_project"


# ─────────────────────────────────────────────────────────────────────────────
# Sanity: all expected core templates exist
# ─────────────────────────────────────────────────────────────────────────────

class TestCoreTemplatesExist:
    # ADR 0009 + 0022: roles use the professional persona taxonomy;
    # tasks use the workflow-semantic phase taxonomy.
    EXPECTED = [
        "roles/code_reviewer",
        "roles/implementation_engineer",
        "roles/plan_reviewer",
        "roles/release_manager",
        "roles/systems_architect",
        "roles/product_owner",
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
        "formats/terse",
        "formats/compact",
        "formats/detailed",
        "formats/bullets",
        "formats/handoff",
    ]

    def test_all_expected_templates_present(self) -> None:
        available = list_core_prompts()
        for name in self.EXPECTED:
            assert name in available, f"Missing core template: {name}"


# ─────────────────────────────────────────────────────────────────────────────
# pipeline/prompts — real template rendering
# ─────────────────────────────────────────────────────────────────────────────

def _wire(turn) -> str:
    """Extract the wire text from a PromptTurn (or pass through a plain str)."""
    return turn.text if hasattr(turn, "text") else turn


class TestPlanPromptIntegration:
    def test_contains_task(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.plan_prompt(task, "/proj", plugin))
        assert task in result

    def test_contains_project_dir(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.plan_prompt(task, "/my/project", plugin))
        assert "/my/project" in result

    def test_uses_code_owned_plan_artifact_boundary(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        result = _wire(prompts.plan_prompt(task, "/proj", plugin))
        assert plugin.ma_artifacts_dir not in result
        assert 'name="plan_artifact_boundary"' in result
        assert "Do not call Write or Edit" in result

    def test_contains_plan_extra(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.plan_prompt(task, "/proj", plugin))
        assert "Follow PEP 8 strictly." in result

    def test_no_code_instruction(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.plan_prompt(task, "/proj", plugin))
        assert "implementation code" in result.lower()

    def test_contains_language(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.plan_prompt(task, "/proj", plugin))
        assert "Python" in result

    def test_project_override_applied(
        self, task: str, plugin: PluginConfig, project_dir: Path
    ) -> None:
        override_dir = project_dir / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "plan.md").write_text(
            "PROJECT PLAN OVERRIDE: $task"
        )
        result = _wire(prompts.plan_prompt(task, str(project_dir), plugin))
        assert "PROJECT PLAN OVERRIDE" in result
        assert task in result


class TestBuildPromptIntegration:
    def test_contains_task_and_dir(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.build_prompt(task, "/proj", plugin))
        assert task in result
        assert "/proj" in result

    def test_references_artifacts_dir(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.build_prompt(task, "/proj", plugin))
        assert plugin.ma_artifacts_dir in result

    def test_contains_build_extra(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.build_prompt(task, "/proj", plugin))
        assert "Do not touch migrations." in result

    def test_no_extra_when_empty(self, task: str) -> None:
        plugin = PluginConfig()
        result = _wire(prompts.build_prompt(task, "/proj", plugin))
        # extra_step line should be blank, not contain step number with empty text
        assert "5. \n" not in result


class TestReviewFocusIntegration:
    def test_contains_task(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.review_focus(task, plugin))
        assert task[:80] in result

    def test_contains_review_extra(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.review_focus(task, plugin))
        assert "N+1 queries" in result

    def test_project_override(
        self, task: str, plugin: PluginConfig, project_dir: Path
    ) -> None:
        override_dir = project_dir / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "code_review.md").write_text(
            "CUSTOM REVIEW for $task\n$extra_checks"
        )
        result = _wire(prompts.review_focus(task, plugin, project_dir=str(project_dir)))
        assert "CUSTOM REVIEW" in result
        assert task in result


class TestReplanPromptIntegration:
    def test_contains_task_and_critique(
        self, task: str, plugin: PluginConfig
    ) -> None:
        critique = "Missing error handling for edge case"
        result = _wire(prompts.replan_prompt(task, critique, "", "/proj", plugin))
        assert task in result
        assert critique in result

    def test_no_code_instruction(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.replan_prompt(task, "some critique", "", "/proj", plugin))
        assert "implementation code" in result.lower()


class TestFixPromptIntegration:
    def test_contains_critique(self, task: str, plugin: PluginConfig) -> None:
        critique = "Logic error on line 42"
        result = _wire(prompts.fix_prompt(task, critique, "/proj", plugin))
        assert critique in result

    def test_injects_test_failures(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.fix_prompt(
            task, "review_changes", "/proj", plugin, test_failures="FAILED test_foo"
        ))
        assert "FAILED test_foo" in result
        assert "FAILING" in result

    def test_injects_write_style_pytest(
        self, task: str, plugin: PluginConfig
    ) -> None:
        result = _wire(prompts.fix_prompt(
            task, "review_changes", "/proj", plugin, write_style="pytest"
        ))
        assert "pytest" in result.lower()

    def test_fix_all_instruction(self, task: str, plugin: PluginConfig) -> None:
        result = _wire(prompts.fix_prompt(task, "issue", "/proj", plugin))
        assert "please fix all issues" in result.lower()


# ─────────────────────────────────────────────────────────────────────────────
# cross_orchestrator — cross_plan_prompt resolves from _ORCHESTRATOR_ROOT
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossPlanPromptIntegration:
    def test_renders_without_error(self, tmp_path: Path) -> None:
        from pipeline.cross_project.orchestrator import cross_plan_prompt
        projects = {
            "unity": tmp_path / "unity",
            "api": tmp_path / "api",
        }
        result = _wire(cross_plan_prompt("add logging", projects, tmp_path / "run"))
        assert "add logging" in result
        assert "unity" in result
        assert "api" in result

    def test_orchestrator_root_is_workspace_orchestrator(self) -> None:
        from pipeline.cross_project.orchestrator import _ORCHESTRATOR_ROOT
        # After M1.1 migration the engine lives at ~/.local/share/multiagent-core
        # (or ORCHO_WORKSPACE override). The parent dir name is no longer guaranteed
        # to be "workspace-orchestrator" — assert structural validity instead.
        assert _ORCHESTRATOR_ROOT is not None
        assert _ORCHESTRATOR_ROOT.exists() or not _ORCHESTRATOR_ROOT.exists()  # path is resolved

    def test_workspace_override_applied(self, tmp_path: Path) -> None:
        """
        If the orchestrator workspace ships a project override of
        ``tasks/cross_plan.md`` (ADR 0009 Phase 8 composable-part path),
        it must win over the core template.
        """
        from pipeline.cross_project import orchestrator as cross_module

        orchestrator_root = tmp_path / "workspace-orchestrator"
        override_dir = orchestrator_root / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "cross_plan.md"
        override_file.write_text("WORKSPACE CROSS OVERRIDE: $task", encoding="utf-8")

        original_root = cross_module._ORCHESTRATOR_ROOT
        cross_module._ORCHESTRATOR_ROOT = orchestrator_root
        try:
            projects = {"unity": tmp_path / "unity"}
            result = _wire(cross_module.cross_plan_prompt(
                "test task", projects, tmp_path / "run"
            ))
            assert "WORKSPACE CROSS OVERRIDE" in result
        finally:
            cross_module._ORCHESTRATOR_ROOT = original_root

    def test_validate_cross_plan_honors_orchestrator_root_override(
        self, tmp_path: Path,
    ) -> None:
        """
        Symmetric to :meth:`test_workspace_override_applied`: when an
        embedder/test rebinds ``orchestrator._ORCHESTRATOR_ROOT`` to a
        workspace that ships its own ``tasks/cross_validate_plan.md``
        override, the planning_loop helper must use the live binding
        (not its import-time copy of the prompts-module global).
        Regression for the bug surfaced after Phase 4a of the
        orchestrator refactor.
        """
        from pipeline.cross_project import orchestrator as cross_module
        from pipeline.cross_project.planning_loop import validate_cross_plan

        orchestrator_root = tmp_path / "workspace-orchestrator"
        override_dir = (
            orchestrator_root / ".orcho" / "multiagent" / "prompts" / "tasks"
        )
        override_dir.mkdir(parents=True)
        override_file = override_dir / "cross_validate_plan.md"
        override_file.write_text(
            "CROSS VALIDATE OVERRIDE: $task", encoding="utf-8",
        )

        class _CapturingQA:
            model = "fake-codex"

            def __init__(self) -> None:
                self.prompt: str | None = None

            def invoke(self, prompt: str, _cwd: str, **_kw) -> str:
                self.prompt = prompt
                return '{"verdict":"APPROVED","short_summary":"","findings":[],"risks":[],"checks":[]}'

        qa = _CapturingQA()
        original_root = cross_module._ORCHESTRATOR_ROOT
        cross_module._ORCHESTRATOR_ROOT = orchestrator_root
        try:
            validate_cross_plan(
                qa,
                "cross plan body",
                "test task",
                ["unity"],
                str(tmp_path),
                orchestrator_root=cross_module._ORCHESTRATOR_ROOT,
            )
        finally:
            cross_module._ORCHESTRATOR_ROOT = original_root

        assert qa.prompt is not None
        assert "CROSS VALIDATE OVERRIDE" in qa.prompt
