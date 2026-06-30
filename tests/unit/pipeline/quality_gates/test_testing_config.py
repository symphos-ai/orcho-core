"""
TestingConfig + run_tests() shell step.

Covers:
 run_tests skip when run_command is None (TestResult.skipped=True)
 pass path (returncode=0, no fail_keyword)
 fail path via non-zero exit
 fail path via fail_keyword in stdout (returncode=0)
 timeout path
 build_fix_prompt formatting for Behat / pytest / NUnit / xUnit
 fix_prompt forwards test_failures + write_style to build_fix_prompt
"""

import subprocess
from unittest.mock import MagicMock, patch

from agents.entities import TestResult
from pipeline import prompts
from pipeline.plugins import PluginConfig, TestingConfig
from pipeline.project_testing import run_tests

# ── run_tests: skip path ──────────────────────────────────────────────────────

class TestRunTestsSkip:
    def test_skip_when_run_command_is_none(self, project_dir: str) -> None:
        plugin = PluginConfig(quality_gates={"tests": {"run_command": None}})
        result = run_tests(project_dir, plugin)
        assert result.skipped is True
        assert result.passed is True       # skipped is success-by-default
        assert result.failed is False
        assert result.output == ""

    def test_skip_when_run_command_is_empty_string(self, project_dir: str) -> None:
        plugin = PluginConfig(quality_gates={"tests": {"run_command": ""}})
        result = run_tests(project_dir, plugin)
        assert result.skipped is True


# ── run_tests: pass path ──────────────────────────────────────────────────────

class TestRunTestsPass:
    def test_passes_on_returncode_zero(self, project_dir: str) -> None:
        plugin = PluginConfig(
            quality_gates={"tests": {"run_command": "echo passing", "fail_keyword": "failed"}}
        )
        fake = MagicMock(returncode=0, stdout="3 tests passed\n", stderr="")
        with patch("pipeline.project_testing.subprocess.run", return_value=fake) as m:
            result = run_tests(project_dir, plugin)
        assert result.skipped is False
        assert result.passed is True
        assert result.failed is False
        assert "3 tests passed" in result.output
        # shell=True is required so plugin authors can pass full command lines
        assert m.call_args.kwargs.get("shell") is True

    def test_fail_keyword_absent_in_passing_output(self, project_dir: str) -> None:
        """fail_keyword must not match successful output like '0 failed'.
 We only check for the literal substring, so the plugin author is
 responsible for picking a keyword that won't show up in clean runs."""
        plugin = PluginConfig(
            quality_gates={"tests": {"run_command": "x", "fail_keyword": "FATAL"}},
        )
        fake = MagicMock(returncode=0, stdout="all green, 0 failed\n", stderr="")
        with patch("pipeline.project_testing.subprocess.run", return_value=fake):
            result = run_tests(project_dir, plugin)
        assert result.passed is True


# ── run_tests: fail path ──────────────────────────────────────────────────────

class TestRunTestsFail:
    def test_fails_on_nonzero_returncode(self, project_dir: str) -> None:
        plugin = PluginConfig(
            quality_gates={"tests": {"run_command": "false", "fail_keyword": "failed"}},
        )
        fake = MagicMock(
            returncode=1,
            stdout="2 passed, 1 failed\n",
            stderr="AssertionError",
        )
        with patch("pipeline.project_testing.subprocess.run", return_value=fake):
            result = run_tests(project_dir, plugin)
        assert result.skipped is False
        assert result.passed is False
        assert result.failed is True
        # output combines stdout + stderr so the FIX prompt sees both
        assert "2 passed, 1 failed" in result.output
        assert "AssertionError" in result.output

    def test_fail_keyword_triggers_failure_even_when_returncode_zero(
        self, project_dir: str
    ) -> None:
        """Some runners exit 0 with 'X failed' in output — keyword catches that."""
        plugin = PluginConfig(
            quality_gates={"tests": {"run_command": "x", "fail_keyword": "failed"}},
        )
        fake = MagicMock(returncode=0, stdout="3 failed\n", stderr="")
        with patch("pipeline.project_testing.subprocess.run", return_value=fake):
            result = run_tests(project_dir, plugin)
        assert result.passed is False
        assert result.failed is True


# ── run_tests: timeout ────────────────────────────────────────────────────────

class TestRunTestsTimeout:
    def test_timeout_returns_failed_result_with_marker(self, project_dir: str) -> None:
        plugin = PluginConfig(
            quality_gates={"tests": {"run_command": "sleep 999", "timeout": 1}},
        )
        with patch(
            "pipeline.project_testing.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sleep 999", timeout=1),
        ):
            result = run_tests(project_dir, plugin)
        assert result.skipped is False
        assert result.passed is False
        assert result.failed is True
        assert "[TIMEOUT" in result.output


# ── build_fix_prompt formatting ───────────────────────────────────────────────

class TestBuildFixPrompt:
    def test_review_only(self) -> None:
        out = prompts.build_fix_prompt(review="missing null check")
        assert "missing null check" in out
        # No test block when there are no failures.
        assert "test suite is FAILING" not in out
        # Always ends with the closing instruction.
        assert "Please fix all issues" in out

    def test_review_plus_test_failures(self) -> None:
        out = prompts.build_fix_prompt(
            review="logic error in foo()",
            test_failures="FAIL test_foo.py::test_bar  AssertionError",
        )
        assert "logic error in foo()" in out
        assert "test_foo.py::test_bar" in out
        assert "test suite is FAILING" in out

    def test_write_style_pytest_hint(self) -> None:
        out = prompts.build_fix_prompt(review="x", write_style="pytest")
        assert "pytest" in out.lower()

    def test_write_style_behat_hint(self) -> None:
        out = prompts.build_fix_prompt(review="x", write_style="behat_gherkin")
        assert "Behat" in out or "Gherkin" in out

    def test_write_style_nunit_hint(self) -> None:
        out = prompts.build_fix_prompt(review="x", write_style="nunit_csharp")
        assert "NUnit" in out or "[Test]" in out

    def test_write_style_xunit_hint(self) -> None:
        out = prompts.build_fix_prompt(review="x", write_style="xunit_csharp")
        assert "xUnit" in out or "[Fact]" in out

    def test_write_style_unknown_falls_back_to_passthrough(self) -> None:
        out = prompts.build_fix_prompt(review="x", write_style="rspec_ruby")
        assert "rspec_ruby" in out


# ── fix_prompt integration with test failures ────────────────────────────────

class TestFixPromptWithTestFailures:
    def test_fix_prompt_embeds_test_failures(
        self, task: str, full_plugin: PluginConfig
    ) -> None:
        p = prompts.fix_prompt(
            task,
            "logic bug at line 42",
            "/project",
            full_plugin,
            test_failures="FAIL test_foo.py::test_bar",
            write_style="pytest",
        ).text
        assert "logic bug at line 42" in p
        assert "test_foo.py::test_bar" in p
        assert "pytest" in p.lower()

    def test_fix_prompt_without_kwargs_unchanged_behavior(
        self, task: str, full_plugin: PluginConfig
    ) -> None:
        """Old call sites that don't pass test_failures/write_style still work
 and don't include any test-suite block."""
        p = prompts.fix_prompt(task, "issue", "/project", full_plugin).text
        assert "issue" in p
        assert "test suite is FAILING" not in p


# ── TestingConfig defaults ────────────────────────────────────────────────────

class TestTestingConfigDefaults:
    def test_default_is_skipped(self) -> None:
        cfg = TestingConfig()
        assert cfg.run_command is None
        assert cfg.write_style == ""
        assert cfg.fail_keyword == "failed"
        assert cfg.timeout == 120

    def test_pluginconfig_default_quality_gates_empty(self) -> None:
        """``PluginConfig.testing`` field deleted.
 Default ``quality_gates`` is empty dict; ``_resolve_tests_config``
 returns a skipped TestingConfig."""
        from pipeline.project_testing import resolve_tests_config as _resolve_tests_config
        plugin = PluginConfig()
        cfg = _resolve_tests_config(plugin)
        assert cfg.run_command is None
        assert plugin.quality_gates == {}

    def test_plugin_loader_quality_gates_dict_resolved_to_testing_config(
        self, project_dir: str
    ) -> None:
        """customer plugins now use
 ``PLUGIN["quality_gates"] = {"tests": {...}}`` shape. Coercion
 from dict to internal ``TestingConfig`` happens at gate-firing
 time via ``_resolve_tests_config()``."""
        from pathlib import Path

        from pipeline.plugins import load_plugin
        from pipeline.project_testing import resolve_tests_config as _resolve_tests_config

        plugin_dir = Path(project_dir) / ".orcho" / "multiagent"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.py").write_text(
            "PLUGIN = {"
            "  'name': 'X',"
            "  'quality_gates': {"
            "    'tests': {'run_command': 'pytest -q', 'write_style': 'pytest'}"
            "  }"
            "}"
        )
        plugin = load_plugin(project_dir)
        cfg = _resolve_tests_config(plugin)
        assert isinstance(cfg, TestingConfig)
        assert cfg.run_command == "pytest -q"
        assert cfg.write_style == "pytest"


# ── TestResult helpers ────────────────────────────────────────────────────────

class TestTestResult:
    def test_skipped_is_not_failed(self) -> None:
        assert TestResult(skipped=True).failed is False

    def test_passed_is_not_failed(self) -> None:
        assert TestResult(passed=True).failed is False

    def test_failed_when_not_skipped_and_not_passed(self) -> None:
        assert TestResult(skipped=False, passed=False).failed is True
