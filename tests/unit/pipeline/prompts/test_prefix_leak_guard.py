"""M10 prefix-leak guard: rendered dynamic text never lands in cacheable prefix.

The M10 prose rewrite shrinks editable prompts but the structural
rule that backs it is M2's: any rendered text that depends on the
turn (task body, reviewed artifact, prior critique, diff, test
output, file path, round number) MUST appear only in turn-payload
parts, never in STATIC/GLOBAL prefix parts the M6 selector might
omit on a resumed session.

The composer enforces this metadata-side via its
``_TURN_VARIABLES`` scan, but a future prose edit could move a
``$task`` substitution into a role/format file (which currently
has no turn variables and therefore renders as STATIC/GLOBAL) and
silently leak the task body into the cacheable prefix. This test
guards against that drift end-to-end: it runs each shipped builder
with a sentinel task / artifact / critique value and asserts the
sentinel never appears in any envelope part the partitioner placed
in the stable prefix.
"""

from __future__ import annotations

import pytest

from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.envelope import is_prefix_eligible

# Sentinel strings chosen to be improbable in any role/task/format
# prose. If any of these appear in a STATIC/GLOBAL prefix part, the
# composer or the rewritten prompts let dynamic text leak.
_SENTINEL_TASK = "ZZSENTINELTASKZZ-task-body-text-78f3e2b4"
_SENTINEL_ARTIFACT = "ZZSENTINELARTIFACTZZ-artifact-content-c81a7d05"
_SENTINEL_CRITIQUE = "ZZSENTINELCRITIQUEZZ-prior-critique-19e5f02c"






def _assert_no_sentinel_in_prefix(env, sentinel: str) -> None:
    """Every prefix-eligible part's body must NOT contain ``sentinel``.

    The body strings are the rendered text the agent saw on the wire
    for that part. M2's contiguous-prefix rule places only
    prefix-eligible parts in ``stable_prefix_parts``; this guard
    re-checks that filter directly so the test fails for the right
    reason (a STATIC part carrying turn data) rather than an
    ordering quirk.
    """
    leaks = []
    for part in env.parts:
        if not is_prefix_eligible(part):
            continue
        if sentinel in part.body:
            leaks.append((part.kind, part.name, part.id))
    assert not leaks, (
        f"sentinel {sentinel!r} leaked into prefix-eligible part(s): {leaks}"
    )


# ---------------------------------------------------------------------------
# Builder coverage. Each shipped phase that takes a task / artifact /
# critique gets a sentinel-stamped run and a per-sentinel prefix check.
# ---------------------------------------------------------------------------


class TestPlanBuildersPrefixSafety:
    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_plan_prompt_keeps_task_text_out_of_prefix(
        self, plugin: PluginConfig,
    ) -> None:
        _env_tmp = prompts.plan_prompt(_SENTINEL_TASK, "/proj", plugin).envelope()
        _assert_no_sentinel_in_prefix(_env_tmp, _SENTINEL_TASK)

    def test_replan_prompt_keeps_task_and_critique_out_of_prefix(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.replan_prompt(
            _SENTINEL_TASK,
            _SENTINEL_CRITIQUE,
            "",
            "/proj",
            plugin,
        ).envelope()
        _assert_no_sentinel_in_prefix(env, _SENTINEL_TASK)
        _assert_no_sentinel_in_prefix(env, _SENTINEL_CRITIQUE)

    def test_hypothesis_prompt_keeps_task_text_out_of_prefix(self) -> None:
        _env_tmp = prompts.hypothesis_prompt(_SENTINEL_TASK, "/proj", codemap="x.py").envelope()
        _assert_no_sentinel_in_prefix(_env_tmp, _SENTINEL_TASK)


class TestImplementBuildersPrefixSafety:
    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_build_prompt_keeps_task_text_out_of_prefix(
        self, plugin: PluginConfig,
    ) -> None:
        _env_tmp = prompts.build_prompt(_SENTINEL_TASK, "/proj", plugin).envelope()
        _assert_no_sentinel_in_prefix(_env_tmp, _SENTINEL_TASK)

    def test_fix_prompt_keeps_task_and_critique_out_of_prefix(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.fix_prompt(
            _SENTINEL_TASK,
            _SENTINEL_CRITIQUE,
            "/proj",
            plugin,
        ).envelope()
        _assert_no_sentinel_in_prefix(env, _SENTINEL_TASK)
        _assert_no_sentinel_in_prefix(env, _SENTINEL_CRITIQUE)


class TestReviewBuildersPrefixSafety:
    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_review_focus_keeps_task_text_out_of_prefix(
        self, plugin: PluginConfig,
    ) -> None:
        _env_tmp = prompts.review_focus(_SENTINEL_TASK, plugin, "/proj").envelope()
        _assert_no_sentinel_in_prefix(_env_tmp, _SENTINEL_TASK)

    def test_plan_review_focus_keeps_task_text_out_of_prefix(
        self, plugin: PluginConfig,
    ) -> None:
        _env_tmp = prompts.plan_review_focus(_SENTINEL_TASK, plugin, "/proj").envelope()
        _assert_no_sentinel_in_prefix(_env_tmp, _SENTINEL_TASK)


class TestFileValidationBuildersPrefixSafety:
    """The M4 dynamic artifact part is the load-bearing case: it
    carries the reviewed file body and must always land in turn
    payload, never in the prefix. A regression here would silently
    cache reviewed artifact content across rounds and corrupt M6
    delta selection."""

    def test_hypothesis_file_review_keeps_artifact_out_of_prefix(self) -> None:
        env = prompts.hypothesis_file_review_prompt(
            "/tmp/h.md",
            _SENTINEL_ARTIFACT,
            _SENTINEL_TASK,
            project_dir="/proj",
        ).envelope()
        _assert_no_sentinel_in_prefix(env, _SENTINEL_TASK)
        _assert_no_sentinel_in_prefix(env, _SENTINEL_ARTIFACT)

    def test_plan_file_review_keeps_artifact_out_of_prefix(self) -> None:
        """PR3: ``plan_file_review_prompt`` emits typed plan views
        (plan_contract:typed_plan + plan_tasks:execution_plan, both
        TURN/NONE) rendered from ParsedPlan. Body content from the
        plan (carried by ``acceptance_criteria`` and per-task
        ``spec``) must land in turn-payload, never in the stable
        prefix — otherwise a per-round plan body would silently
        cache and corrupt M6 delta selection on round 2."""
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        plan = ParsedPlan(
            short_summary="Stub plan.",
            planning_context="Stub.",
            subtasks=(
                SubTask(
                    id="t1", goal="Demo goal",
                    spec=_SENTINEL_ARTIFACT,
                ),
            ),
            source="json",
            acceptance_criteria=(_SENTINEL_ARTIFACT,),
        )
        env = prompts.plan_file_review_prompt(
            plan,
            _SENTINEL_TASK,
            PluginConfig(),
            project_dir="/proj",
        ).envelope()
        _assert_no_sentinel_in_prefix(env, _SENTINEL_TASK)
        _assert_no_sentinel_in_prefix(env, _SENTINEL_ARTIFACT)


# ── M12-C4: metadata-complement guard ────────────────────────────────────────
#
# The sentinel-body checks above catch a STATIC/GLOBAL part that
# accidentally renders dynamic text. They are kept as the primary
# defence — body-level scans see real leaked content even when the
# metadata is correct.
#
# The metadata complement below catches a structural classification
# bug: a part whose body comes from a project/run-dependent template
# (``$project_dir`` / ``$context`` substituted text) but whose
# metadata still claims STATIC/GLOBAL. The M11.5 Fix 3 classifier
# closes this on the composer side; this guard re-checks it from the
# envelope side so a future refactor of the classifier cannot
# silently regress.


_PROJECT_DIR_MARKER = "/sentinel-project-dir-c1c1c1"


def _walk_static_global_parts(env):
    """Yield every part whose metadata claims STATIC + GLOBAL."""
    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptStability,
    )

    for part in env.parts:
        if (
            part.stability is PromptStability.STATIC
            and part.cache_scope is PromptCacheScope.GLOBAL
        ):
            yield part


class TestMetadataComplementPrefixGuard:
    """Walk every shipped builder's envelope, find STATIC/GLOBAL
    parts, and assert they carry no project/run substitution
    markers. Complements the body-level sentinel checks; does not
    replace them.
    """

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_plan_static_global_parts_carry_no_project_dir(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.plan_prompt(
            _SENTINEL_TASK, _PROJECT_DIR_MARKER, plugin,
        ).envelope()
        for part in _walk_static_global_parts(env):
            assert _PROJECT_DIR_MARKER not in part.body, (
                f"STATIC/GLOBAL part {part.id!r} carries the "
                f"project-dir marker; the M11.5 classifier should "
                f"have demoted it to RUN/WORKSPACE."
            )

    def test_replan_static_global_parts_carry_no_project_dir(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.replan_prompt(
            _SENTINEL_TASK,
            _SENTINEL_CRITIQUE,
            "",
            _PROJECT_DIR_MARKER,
            plugin,
        ).envelope()
        for part in _walk_static_global_parts(env):
            assert _PROJECT_DIR_MARKER not in part.body

    def test_build_prompt_static_global_parts_carry_no_project_dir(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.build_prompt(
            _SENTINEL_TASK, _PROJECT_DIR_MARKER, plugin,
        ).envelope()
        for part in _walk_static_global_parts(env):
            assert _PROJECT_DIR_MARKER not in part.body

    def test_fix_prompt_static_global_parts_carry_no_project_dir(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.fix_prompt(
            _SENTINEL_TASK,
            _SENTINEL_CRITIQUE,
            _PROJECT_DIR_MARKER,
            plugin,
        ).envelope()
        for part in _walk_static_global_parts(env):
            assert _PROJECT_DIR_MARKER not in part.body

    def test_review_focus_static_global_parts_carry_no_project_dir(
        self, plugin: PluginConfig,
    ) -> None:
        env = prompts.review_focus(_SENTINEL_TASK, plugin, _PROJECT_DIR_MARKER).envelope()
        for part in _walk_static_global_parts(env):
            assert _PROJECT_DIR_MARKER not in part.body
