"""
Coverage for the internal professional-prompt ablation mode (see
``pipeline.prompts.modes``).

Three planes:

1. **Mode coercion** — ``coerce_professional_prompt_mode`` accepts
 ``None``, ``"full"``, ``"minimal"``, the enum itself, and rejects
 junk with ``ValueError``.
2. **Default unchanged** — every builder produces the same string for
 ``professional_prompt_mode=None`` and ``professional_prompt_mode="full"``.
 That guarantees the ablation kwarg is a pure opt-in for eval / tests
 and existing callers (which never pass it) keep the current behavior.
3. **Minimal mode shape** — for every builder with a distinctive
 role/task anchor, the minimal render skips that anchor while
 preserving required phase inputs and the system-tail contracts.

System-tail must always remain attached — disabling it would test
protocol breakage, not prompt quality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import prompts
from pipeline.cross_project.orchestrator import cross_plan_prompt
from pipeline.plugins import PluginConfig
from pipeline.prompts.modes import (
    ProfessionalPromptMode,
    coerce_professional_prompt_mode,
)

# ---------------------------------------------------------------------------
# Plane 1: mode coercion.
# ---------------------------------------------------------------------------


class TestCoerceProfessionalPromptMode:
    def test_none_defaults_to_full(self) -> None:
        assert coerce_professional_prompt_mode(None) is ProfessionalPromptMode.FULL

    def test_full_string_resolves(self) -> None:
        assert coerce_professional_prompt_mode("full") is ProfessionalPromptMode.FULL

    def test_minimal_string_resolves(self) -> None:
        assert (
            coerce_professional_prompt_mode("minimal") is ProfessionalPromptMode.MINIMAL
        )

    def test_case_insensitive(self) -> None:
        assert coerce_professional_prompt_mode("FULL") is ProfessionalPromptMode.FULL
        assert (
            coerce_professional_prompt_mode("Minimal") is ProfessionalPromptMode.MINIMAL
        )

    def test_enum_value_passes_through(self) -> None:
        assert (
            coerce_professional_prompt_mode(ProfessionalPromptMode.MINIMAL)
            is ProfessionalPromptMode.MINIMAL
        )

    def test_unknown_string_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown professional_prompt_mode"):
            coerce_professional_prompt_mode("partial")

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported professional_prompt_mode type"):
            coerce_professional_prompt_mode(42)  # type: ignore[arg-type]

    def test_minimal_with_format_string_resolves(self) -> None:
        """A4.5: three-way ablation adds ``minimal_with_format`` between
 full and minimal."""
        assert (
            coerce_professional_prompt_mode("minimal_with_format")
            is ProfessionalPromptMode.MINIMAL_WITH_FORMAT
        )

    def test_minimal_with_format_case_insensitive(self) -> None:
        assert (
            coerce_professional_prompt_mode("Minimal_With_Format")
            is ProfessionalPromptMode.MINIMAL_WITH_FORMAT
        )


# ---------------------------------------------------------------------------
# Plane 2: default unchanged — None == "full".
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin() -> PluginConfig:
    return PluginConfig(
        name="Test",
        language="Python",
        architecture="FastAPI",
        ma_artifacts_dir=".orcho/artifacts",
        file_hints=["src/"],
        plan_prompt_extra="Project rule X.",
        build_prompt_extra="Project rule Y.",
        review_focus_extra="Project rule Z.",
    )


@pytest.fixture
def task() -> str:
    return "Add structured logging to the auth service"


class TestDefaultModeUnchanged:
    """``professional_prompt_mode=None`` must equal ``"full"`` byte-for-byte."""

    def test_plan_prompt_default_equals_full(self, task: str, plugin: PluginConfig) -> None:
        a = prompts.plan_prompt(task, "/proj", plugin)
        b = prompts.plan_prompt(task, "/proj", plugin, professional_prompt_mode="full")
        assert a == b

    def test_build_prompt_default_equals_full(self, task: str, plugin: PluginConfig) -> None:
        a = prompts.build_prompt(task, "/proj", plugin)
        b = prompts.build_prompt(task, "/proj", plugin, professional_prompt_mode="full")
        assert a == b

    def test_fix_prompt_default_equals_full(self, task: str, plugin: PluginConfig) -> None:
        a = prompts.fix_prompt(task, "critique", "/proj", plugin)
        b = prompts.fix_prompt(
            task, "critique", "/proj", plugin, professional_prompt_mode="full",
        )
        assert a == b

    def test_replan_prompt_default_equals_full(self, task: str, plugin: PluginConfig) -> None:
        a = prompts.replan_prompt(task, "crit", "", "/proj", plugin)
        b = prompts.replan_prompt(
            task, "crit", "", "/proj", plugin, professional_prompt_mode="full",
        )
        assert a == b

    def test_review_focus_default_equals_full(self, task: str, plugin: PluginConfig) -> None:
        a = prompts.review_focus(task, plugin)
        b = prompts.review_focus(task, plugin, professional_prompt_mode="full")
        assert a == b

    def test_plan_review_focus_default_equals_full(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        a = prompts.plan_review_focus(task, plugin)
        b = prompts.plan_review_focus(task, plugin, professional_prompt_mode="full")
        assert a == b

    def test_hypothesis_prompt_default_equals_full(self, task: str) -> None:
        a = prompts.hypothesis_prompt(task, "/proj")
        b = prompts.hypothesis_prompt(task, "/proj", professional_prompt_mode="full")
        assert a == b

    def test_cross_plan_prompt_default_equals_full(
        self, task: str, tmp_path: Path,
    ) -> None:
        projects = {"api": tmp_path / "api", "web": tmp_path / "web"}
        a = cross_plan_prompt(task, projects, tmp_path / "artifacts")
        b = cross_plan_prompt(
            task, projects, tmp_path / "artifacts",
            professional_prompt_mode="full",
        )
        assert a == b


# ---------------------------------------------------------------------------
# Plane 3: minimal mode — skips professional parts, keeps inputs + system-tail.
# ---------------------------------------------------------------------------


# Anchors unique to the professional layer for each builder. If these appear
# in minimal mode, the role/task content leaked through.
_BUILD_ROLE_ANCHOR = "You are the implementation engineer for this task."
_BUILD_TASK_ANCHOR = "smallest coherent implementation path"
_REVIEW_ROLE_ANCHOR = "You are the application architect for this task."
_REVIEW_TASK_ANCHOR = "look hardest at the failure modes"
_PLAN_ROLE_ANCHOR = "You are the solution architect for this task."
_PLAN_TASK_ANCHOR = "Identify the load-bearing surfaces"
_HYPOTHESIS_QA_TASK_ANCHOR = "Names the riskiest assumption"
_CROSS_PLAN_TASK_ANCHOR = "## Interface Contract"


class TestMinimalModeBuild:
    def test_minimal_skips_professional_parts(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.build_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        assert _BUILD_ROLE_ANCHOR not in out
        assert _BUILD_TASK_ANCHOR not in out

    def test_minimal_preserves_task_input(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.build_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        assert "TASK:" in out
        assert task in out

    def test_minimal_preserves_artifacts_dir(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.build_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        assert plugin.ma_artifacts_dir in out

    def test_minimal_preserves_system_tail(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.build_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        # change_handoff stays attached in minimal mode.
        assert 'name="change_handoff"' in out


class TestMinimalModeReview:
    def test_minimal_skips_professional_parts(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus(task, plugin, professional_prompt_mode="minimal").text
        assert _REVIEW_ROLE_ANCHOR not in out
        assert _REVIEW_TASK_ANCHOR not in out

    def test_minimal_preserves_task(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.review_focus(task, plugin, professional_prompt_mode="minimal").text
        assert task[:80] in out

    def test_minimal_preserves_review_json_contract(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus(task, plugin, professional_prompt_mode="minimal").text
        assert 'name="review_json"' in out

    def test_minimal_preserves_review_target_contract(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus(task, plugin, professional_prompt_mode="minimal").text
        assert 'name="review_target"' in out


class TestMinimalModePlan:
    def test_minimal_skips_professional_parts(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        assert _PLAN_ROLE_ANCHOR not in out
        assert _PLAN_TASK_ANCHOR not in out

    def test_minimal_preserves_plan_json_contract(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        assert 'name="plan_json"' in out

    def test_minimal_preserves_task(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.plan_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        assert task in out


class TestMinimalModeFix:
    def test_minimal_preserves_critique(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.fix_prompt(
            task, "Logic error on line 42", "/proj", plugin,
            professional_prompt_mode="minimal",
        ).text
        assert "Logic error on line 42" in out

    def test_minimal_preserves_test_failures(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.fix_prompt(
            task, "rev", "/proj", plugin,
            test_failures="FAILED test_x",
            professional_prompt_mode="minimal",
        ).text
        assert "FAILED test_x" in out

    def test_minimal_preserves_system_tail(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.fix_prompt(
            task, "rev", "/proj", plugin,
            professional_prompt_mode="minimal",
        ).text
        assert 'name="change_handoff"' in out


class TestMinimalModeHypothesisQA:
    def test_minimal_skips_professional_parts(self, task: str) -> None:
        out = prompts.hypothesis_review_focus(task, professional_prompt_mode="minimal").text
        assert _HYPOTHESIS_QA_TASK_ANCHOR not in out

    def test_minimal_preserves_review_json_contract(self, task: str) -> None:
        out = prompts.hypothesis_review_focus(task, professional_prompt_mode="minimal").text
        assert 'name="review_json"' in out


class TestMinimalWithFormatMode:
    """A4.5 plane: ``minimal_with_format`` strips the role/task method
 layer but keeps the format preset attached. The orthogonality lets
 eval attribute output verbosity to format vs method correctly.

 Anchors per builder:
 BUILD: format=handoff → ``Write for the next agent or maintainer``
 REVIEW: format=detailed → ``Provide a detailed response``
 PLAN: format=detailed → ``Provide a detailed response``
 HYPOTHESIS: format=terse → ``Keep the response concise``
 """

    def test_build_keeps_handoff_format_anchor(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.build_prompt(
            task, "/proj", plugin,
            professional_prompt_mode="minimal_with_format",
        ).text
        # Format anchor present.
        assert "Write for the next agent" in out
        # Role anchor absent.
        assert _BUILD_ROLE_ANCHOR not in out
        # Task anchor absent.
        assert _BUILD_TASK_ANCHOR not in out

    def test_build_preserves_task_and_system_tail(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.build_prompt(
            task, "/proj", plugin,
            professional_prompt_mode="minimal_with_format",
        ).text
        assert task in out
        assert 'name="change_handoff"' in out

    def test_review_keeps_detailed_format_anchor(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus(
            task, plugin, professional_prompt_mode="minimal_with_format",
        ).text
        # detailed.md anchor.
        assert "Provide a detailed response" in out
        # Role + task method anchors absent.
        assert _REVIEW_ROLE_ANCHOR not in out
        assert _REVIEW_TASK_ANCHOR not in out
        # System-tail preserved.
        assert 'name="review_json"' in out
        assert 'name="review_target"' in out

    def test_plan_keeps_detailed_format_anchor(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_prompt(
            task, "/proj", plugin,
            professional_prompt_mode="minimal_with_format",
        ).text
        assert "Provide a detailed response" in out
        assert _PLAN_ROLE_ANCHOR not in out
        assert _PLAN_TASK_ANCHOR not in out
        assert 'name="plan_json"' in out

    def test_hypothesis_keeps_terse_format_anchor(self, task: str) -> None:
        out = prompts.hypothesis_prompt(
            task, "/proj", professional_prompt_mode="minimal_with_format",
        ).text
        # terse.md anchor.
        assert "Keep the response concise" in out

    def test_fix_keeps_handoff_format_anchor(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.fix_prompt(
            task, "Logic error on line 42", "/proj", plugin,
            professional_prompt_mode="minimal_with_format",
        ).text
        assert "Write for the next agent" in out
        # Critique input is still threaded through.
        assert "Logic error on line 42" in out
        # System-tail preserved.
        assert 'name="change_handoff"' in out


class TestThreeWayOrthogonality:
    """A4.5: full vs minimal_with_format vs minimal must form a clean
 gradient — full ⊃ minimal_with_format ⊃ minimal in content."""

    def test_build_three_modes_gradient(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        full = prompts.build_prompt(
            task, "/proj", plugin, professional_prompt_mode="full",
        ).text
        mwf = prompts.build_prompt(
            task, "/proj", plugin,
            professional_prompt_mode="minimal_with_format",
        ).text
        mini = prompts.build_prompt(
            task, "/proj", plugin, professional_prompt_mode="minimal",
        ).text
        # Strict gradient on rendered size: full > mwf > minimal.
        assert len(full) > len(mwf) > len(mini), (
            f"expected full > mwf > minimal, got "
            f"full={len(full)} mwf={len(mwf)} minimal={len(mini)}"
        )

    def test_all_three_modes_preserve_system_tail(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        for mode in ("full", "minimal_with_format", "minimal"):
            out = prompts.build_prompt(
                task, "/proj", plugin, professional_prompt_mode=mode,
            ).text
            assert 'name="change_handoff"' in out, (
                f"mode={mode} dropped system-tail"
            )


class TestMinimalModeCrossPlan:
    def test_minimal_skips_professional_parts(self, task: str, tmp_path: Path) -> None:
        projects = {"api": tmp_path / "api", "web": tmp_path / "web"}
        out = cross_plan_prompt(
            task, projects, tmp_path / "artifacts",
            professional_prompt_mode="minimal",
        ).text
        assert _CROSS_PLAN_TASK_ANCHOR not in out

    def test_minimal_preserves_paths(self, task: str, tmp_path: Path) -> None:
        projects = {"api": tmp_path / "api", "web": tmp_path / "web"}
        out = cross_plan_prompt(
            task, projects, tmp_path / "artifacts",
            professional_prompt_mode="minimal",
        ).text
        assert "api" in out
        assert "web" in out

    def test_minimal_preserves_cross_plan_json_contract(
        self, task: str, tmp_path: Path,
    ) -> None:
        projects = {"api": tmp_path / "api", "web": tmp_path / "web"}
        out = cross_plan_prompt(
            task, projects, tmp_path / "artifacts",
            professional_prompt_mode="minimal",
        ).text
        # ADR 0054: the code-owned JSON contract survives even in minimal
        # mode — the machine-output shape is non-negotiable.
        assert 'name="cross_plan_json"' in out


# ---------------------------------------------------------------------------
# Plane 4: no placeholder leaks in any minimal render.
# ---------------------------------------------------------------------------


_PLACEHOLDER_PATTERNS = (
    "$task",
    "$body",
    "$critique",
    "$extra_checks",
    "$extra_step",
    "$skill_roster_block",
    "$paths_list",
    "$ma_artifacts_dir",
    "$codemap_section",
    "$context",
    "$file_path",
    "$file_content",
    "$focus",
    "$project_dir",
    "$task_language",
    "$cross_artifacts_dir",
    "$aliases",
)


def _assert_no_placeholder_leak(label: str, out: str) -> None:
    for pat in _PLACEHOLDER_PATTERNS:
        assert pat not in out, f"{label}: placeholder {pat!r} leaked into minimal render"


class TestMinimalRenderHasNoPlaceholderLeaks:
    def test_plan(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.plan_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("plan", out)

    def test_replan(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.replan_prompt(task, "c", "", "/proj", plugin, professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("replan", out)

    def test_decompose(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.decompose_plan_prompt(
            task, "/proj", plugin, professional_prompt_mode="minimal",
        ).text
        _assert_no_placeholder_leak("decompose", out)

    def test_build(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.build_prompt(task, "/proj", plugin, professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("implement", out)

    def test_fix(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.fix_prompt(
            task, "c", "/proj", plugin, professional_prompt_mode="minimal",
        ).text
        _assert_no_placeholder_leak("repair_changes", out)

    def test_review_focus(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.review_focus(task, plugin, professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("review_focus", out)

    def test_plan_review_focus(self, task: str, plugin: PluginConfig) -> None:
        out = prompts.plan_review_focus(task, plugin, professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("plan_review_focus", out)

    def test_hypothesis(self, task: str) -> None:
        out = prompts.hypothesis_prompt(task, "/proj", professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("hypothesis", out)

    def test_hypothesis_qa(self, task: str) -> None:
        out = prompts.hypothesis_review_focus(task, professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("validate_hypothesis", out)

    def test_readonly_plan(self, task: str) -> None:
        out = prompts.readonly_plan_prompt(task, "/proj", professional_prompt_mode="minimal").text
        _assert_no_placeholder_leak("readonly_plan", out)

    def test_runtime_review_uncommitted(self) -> None:
        out = prompts.runtime_review_uncommitted_prompt(
            focus="check races", professional_prompt_mode="minimal",
        ).text
        _assert_no_placeholder_leak("runtime_review_uncommitted", out)

    def test_cross_plan(self, task: str, tmp_path: Path) -> None:
        projects = {"api": tmp_path / "api", "web": tmp_path / "web"}
        out = cross_plan_prompt(
            task, projects, tmp_path / "artifacts",
            professional_prompt_mode="minimal",
        ).text
        _assert_no_placeholder_leak("cross_plan", out)
