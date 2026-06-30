"""
Unit tests for core/prompt_loader.py — two-level template resolution.
Tests are isolated: all filesystem access goes through tmp_path fixtures.
No real _prompts/ files are read — templates are created in-test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.io.prompt_loader import (
    _PROJECT_PROMPTS_SUBPATH,
    list_core_prompts,
    list_project_prompts,
    reload_cache,
    render_prompt,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_cache():
    """Flush LRU cache before/after every test to avoid state leakage."""
    reload_cache()
    yield
    reload_cache()


@pytest.fixture
def fake_core(tmp_path: Path, monkeypatch) -> Path:
    """Redirect _CORE_PROMPTS to a fresh tmp dir with fake templates."""
    core_dir = tmp_path / "core_prompts"
    core_dir.mkdir()
    monkeypatch.setattr("core.io.prompt_loader._CORE_PROMPTS", core_dir)
    reload_cache()
    return core_dir


@pytest.fixture
def fake_project(tmp_path: Path) -> Path:
    """A fake project dir with.orcho/multiagent/prompts/ subpath."""
    proj = tmp_path / "my_project"
    override_dir = proj / _PROJECT_PROMPTS_SUBPATH
    override_dir.mkdir(parents=True)
    return proj


# ─────────────────────────────────────────────────────────────────────────────
# render_prompt — core resolution
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderPromptCore:
    def test_renders_core_template(self, fake_core: Path) -> None:
        (fake_core / "my_template.md").write_text("Hello $who!")
        result = render_prompt("my_template", who="World")
        assert result == "Hello World!"

    def test_renders_nested_core_template(self, fake_core: Path) -> None:
        (fake_core / "roles").mkdir()
        (fake_core / "roles" / "code_reviewer.md").write_text("Review $task")
        result = render_prompt("roles/code_reviewer", task="diff")
        assert result == "Review diff"

    def test_substitutes_multiple_vars(self, fake_core: Path) -> None:
        (fake_core / "multi.md").write_text("$a + $b = $c")
        result = render_prompt("multi", a="1", b="2", c="3")
        assert result == "1 + 2 = 3"

    def test_safe_substitute_ignores_unknown(self, fake_core: Path) -> None:
        """Unknown $vars (Claude-facing placeholders) must pass through."""
        (fake_core / "safe.md").write_text("Create plan_<short_name>.md with $task")
        result = render_prompt("safe", task="add logging")
        assert "plan_<short_name>.md" in result
        assert "add logging" in result

    def test_strips_leading_trailing_whitespace(self, fake_core: Path) -> None:
        (fake_core / "padded.md").write_text("\n\n  Content  \n\n")
        result = render_prompt("padded")
        assert result == "Content"

    def test_missing_template_raises(self, fake_core: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            render_prompt("nonexistent_template")

    def test_missing_template_lists_available(self, fake_core: Path) -> None:
        (fake_core / "available_one.md").write_text("x")
        (fake_core / "available_two.md").write_text("x")
        with pytest.raises(FileNotFoundError, match="available_one"):
            render_prompt("missing")

    def test_empty_template_renders_empty(self, fake_core: Path) -> None:
        (fake_core / "empty.md").write_text("")
        assert render_prompt("empty") == ""


# ─────────────────────────────────────────────────────────────────────────────
# render_prompt — project override
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderPromptProjectOverride:
    def test_project_override_wins(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        (fake_core / "implement.md").write_text("CORE: $task")
        override = fake_project / _PROJECT_PROMPTS_SUBPATH / "implement.md"
        override.write_text("PROJECT OVERRIDE: $task")
        result = render_prompt("implement", project_dir=fake_project, task="test")
        assert result == "PROJECT OVERRIDE: test"

    def test_falls_back_to_core_when_no_override(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        (fake_core / "implement.md").write_text("CORE: $task")
        result = render_prompt("implement", project_dir=fake_project, task="test")
        assert result == "CORE: test"

    def test_project_dir_auto_injected_as_variable(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        (fake_core / "tmpl.md").write_text("Working at: $project_dir")
        result = render_prompt("tmpl", project_dir=fake_project)
        assert str(fake_project) in result

    def test_project_dir_str_accepted(self, fake_core: Path, fake_project: Path) -> None:
        (fake_core / "tmpl.md").write_text("$project_dir")
        result = render_prompt("tmpl", project_dir=str(fake_project))
        assert str(fake_project) in result

    def test_explicit_var_not_overwritten_by_auto_inject(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        """setdefault means auto-inject does NOT overwrite a var already in **variables.
 Pass project_dir as a plain variable (not the kwarg) — not possible directly,
 but we can verify the auto-injected value equals str(project_dir)."""
        (fake_core / "tmpl.md").write_text("$project_dir")
        result = render_prompt("tmpl", project_dir=fake_project)
        # auto-injected value must equal str(project_dir)
        assert result == str(fake_project)

    def test_missing_override_missing_core_raises(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            render_prompt("no_such_template", project_dir=fake_project)

    def test_none_project_dir_skips_project_lookup(self, fake_core: Path) -> None:
        (fake_core / "tmpl.md").write_text("CORE")
        result = render_prompt("tmpl", project_dir=None)
        assert result == "CORE"


# ─────────────────────────────────────────────────────────────────────────────
# render_prompt — $project_dir injection detail
# ─────────────────────────────────────────────────────────────────────────────

class TestProjectDirInjection:
    def test_auto_injected_in_project_override(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        override = fake_project / _PROJECT_PROMPTS_SUBPATH / "tmpl.md"
        override.write_text("$project_dir")
        result = render_prompt("tmpl", project_dir=fake_project)
        assert str(fake_project) in result

    def test_auto_injected_in_core_fallback(
        self, fake_core: Path, fake_project: Path
    ) -> None:
        (fake_core / "tmpl.md").write_text("$project_dir")
        result = render_prompt("tmpl", project_dir=fake_project)
        assert str(fake_project) in result


# ─────────────────────────────────────────────────────────────────────────────
# Cache behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestCacheBehaviour:
    def test_template_cached_after_first_load(self, fake_core: Path) -> None:
        (fake_core / "cached.md").write_text("original")
        result1 = render_prompt("cached")
        # mutate file — cache should return old value
        (fake_core / "cached.md").write_text("mutated")
        result2 = render_prompt("cached")
        assert result1 == result2 == "original"

    def test_reload_cache_clears(self, fake_core: Path) -> None:
        (fake_core / "cached.md").write_text("original")
        render_prompt("cached")  # prime cache
        (fake_core / "cached.md").write_text("mutated")
        reload_cache()
        result = render_prompt("cached")
        assert result == "mutated"


# ─────────────────────────────────────────────────────────────────────────────
# list_core_prompts / list_project_prompts
# ─────────────────────────────────────────────────────────────────────────────

class TestListPrompts:
    def test_list_core_prompts(self, fake_core: Path) -> None:
        (fake_core / "alpha.md").write_text("x")
        (fake_core / "beta.md").write_text("x")
        (fake_core / "roles").mkdir()
        (fake_core / "roles" / "code_reviewer.md").write_text("x")
        (fake_core / "README.md").write_text("docs")
        result = list_core_prompts()
        assert "alpha" in result
        assert "beta" in result
        assert "roles/code_reviewer" in result

    def test_list_project_prompts_empty(self, fake_project: Path) -> None:
        assert list_project_prompts(fake_project) == []

    def test_list_project_prompts_with_overrides(self, fake_project: Path) -> None:
        override_dir = fake_project / _PROJECT_PROMPTS_SUBPATH
        (override_dir / "developer_build.md").write_text("x")
        (override_dir / "reviewer_code_review.md").write_text("x")
        result = list_project_prompts(fake_project)
        assert "developer_build" in result
        assert "reviewer_code_review" in result

    def test_list_project_prompts_nonexistent_dir(self, tmp_path: Path) -> None:
        """No.orcho/multiagent/prompts/ → empty list, no error."""
        result = list_project_prompts(tmp_path / "no_such_project")
        assert result == []
