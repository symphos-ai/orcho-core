"""ADR 0028 / M10.5: cache-first physical wire-layout invariants.

These tests pin the load-bearing rules of the cache-first prompt layout:

* role and task prompt files contain only static method prose
  (no ``$<var>`` placeholders); runtime values arrive as typed
  dynamic parts emitted by builders, not via template substitution;
* the project context (``project_dir`` + plugin) is a typed
  ``context:project`` part with ``stability=STATIC`` and
  ``cache_scope=PROJECT``;
* override resolution maps cleanly to the cache tier: workspace
  overrides demote to ``WORKSPACE``, project overrides to
  ``PROJECT``;
* the physical wire prompt begins with cacheable prefix bytes;
* changing only run-volatile values (task text, project_dir) does
  not shift the GLOBAL/WORKSPACE bytes of the prefix.

Tests are distributed across M10.5 commits — each commit ships the
subset its implementation makes green. The full file becomes the
load-bearing guard against re-introducing dynamic bytes at byte 0.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.types import PromptCacheScope, PromptStability

_CORE_ROLES_DIR = Path(__file__).resolve().parents[4] / "core" / "_prompts" / "roles"
_CORE_TASKS_DIR = Path(__file__).resolve().parents[4] / "core" / "_prompts" / "tasks"

# ``string.Template`` accepts both ``$identifier`` and ``${identifier}``.
# Match either form; the file's other ``$`` characters (URLs, etc.)
# are extremely rare in prose and would surface as legitimate misses.
_DOLLAR_VAR = re.compile(r"\$(?:\w+|\{[^}]+\})")


# ---------------------------------------------------------------------------
# Step 1 — roles contain no template variables.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_file",
    sorted(_CORE_TASKS_DIR.glob("*.md")),
    ids=lambda p: p.name,
)
def test_tasks_have_no_template_variables(task_file: Path) -> None:
    # ADR 0028 / M10.5 Step 2 rule 4 (task = skill discipline): task
    # files describe procedure only — they do not embed invocation
    # parameters via ``$<var>`` substitution. Runtime values arrive
    # as typed dynamic parts (``turn_input`` / ``artifact`` /
    # ``feedback``) emitted by builders. One ``$task`` placeholder
    # inside a task file would demote the whole task-method prose
    # (60+ lines) into TURN payload and defeat the cache-first
    # layout.
    text = task_file.read_text(encoding="utf-8")
    matches = _DOLLAR_VAR.findall(text)
    assert not matches, (
        f"task file {task_file.name} contains template variables {matches}; "
        f"ADR 0028 requires task files to be static method prose"
    )


@pytest.mark.parametrize(
    "role_file",
    sorted(_CORE_ROLES_DIR.glob("*.md")),
    ids=lambda p: p.name,
)
def test_roles_have_no_template_variables(role_file: Path) -> None:
    # ADR 0028 / M10.5 rule 3: roles are static persona/posture
    # prose. Runtime values (project_dir, AGENTS echo, plugin
    # context) live in typed parts emitted by builders, not in role
    # file substitution. A ``$<var>`` token in a role file demotes
    # the role part to RUN/WORKSPACE and puts dynamic bytes at the
    # leading edge of the wire prompt — exactly what M10.5 forbids.
    text = role_file.read_text(encoding="utf-8")
    matches = _DOLLAR_VAR.findall(text)
    assert not matches, (
        f"role file {role_file.name} contains template variables {matches}; "
        f"ADR 0028 requires role files to be static prose"
    )


# ---------------------------------------------------------------------------
# Step 1 — project context is a typed STATIC+PROJECT part.
# ---------------------------------------------------------------------------


class TestProjectContextPart:
    def test_project_context_part_is_emitted(self) -> None:
        env = prompts.hypothesis_prompt(
            "Probe", "/work/api-vue", codemap="src.py",
        ).envelope()
        assert env is not None
        ctx_parts = [p for p in env.parts if p.kind == "context"]
        # Exactly one project-context part per render in M10.5.
        assert len(ctx_parts) == 1
        ctx = ctx_parts[0]
        assert ctx.name == "project"
        assert ctx.stability is PromptStability.STATIC
        assert ctx.cache_scope is PromptCacheScope.PROJECT
        # Body/hash invalidation: STATIC parts do not carry a
        # volatile_reason (ADR-0026 validation).
        assert ctx.volatile_reason is None
        assert (
            "You are working directly in the project directory at: /work/api-vue"
            in ctx.body
        )

    def test_project_context_part_marks_active_worktree_checkout(
        self, tmp_path: Path,
    ) -> None:
        from pipeline.engine.worktree import (
            reset_active_worktree_checkout,
            set_active_worktree_checkout,
        )

        checkout = tmp_path / "runs" / "r1" / "checkout"
        checkout.mkdir(parents=True)
        token = set_active_worktree_checkout(str(checkout))
        try:
            env = prompts.hypothesis_prompt(
                "Probe", str(checkout), codemap="src.py",
            ).envelope()
        finally:
            reset_active_worktree_checkout(token)

        assert env is not None
        ctx = next(p for p in env.parts if p.kind == "context")
        assert (
            f"You are working in an isolated git worktree checkout at: {checkout}"
            in ctx.body
        )
        assert "make task changes here, not in the source checkout" in ctx.body
        assert "working directly in the project directory" not in ctx.body

    def test_project_context_part_is_prefix_eligible(self) -> None:
        env = prompts.hypothesis_prompt(
            "Probe", "/work/api-vue", codemap="src.py",
        ).envelope()
        assert env is not None
        ctx = next(
            p for p in env.parts if p.kind == "context" and p.name == "project"
        )
        # STATIC + PROJECT is prefix-eligible under the M10.5
        # envelope rule (envelope.is_prefix_eligible).
        assert ctx in env.stable_prefix_parts

    def test_project_context_body_changes_with_project_dir(self) -> None:
        env_a = prompts.hypothesis_prompt("Probe", "/work/api-vue", codemap="x.py").envelope()

        env_b = prompts.hypothesis_prompt("Probe", "/work/api-php", codemap="x.py").envelope()

        ctx_a = next(p for p in env_a.parts if p.kind == "context")
        ctx_b = next(p for p in env_b.parts if p.kind == "context")
        # Different project_dir → different project_context body
        # (body/hash invalidation drives PROJECT tier eviction).
        assert ctx_a.body != ctx_b.body
        # But role/format bytes survive a project switch — they are
        # GLOBAL and identical across projects.
        role_a = next(p for p in env_a.parts if p.kind == "role")
        role_b = next(p for p in env_b.parts if p.kind == "role")
        assert role_a.body == role_b.body
        assert role_a.cache_scope is PromptCacheScope.GLOBAL


# ---------------------------------------------------------------------------
# Step 1 — context part is suppressed when no project_dir / plugin data.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 4 — override-aware classifier.
# ---------------------------------------------------------------------------


class TestClassifierOverrideAware:
    def _write(self, root: Path, name: str, body: str) -> None:
        target = root / ".orcho" / "multiagent" / "prompts" / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    def test_default_role_classified_global(self, tmp_path: Path) -> None:
        env = prompts.hypothesis_prompt("probe", str(tmp_path), codemap="x.py").envelope()
        role = next(p for p in env.parts if p.kind == "role")
        # Default-shipped role: no $vars, resolution="core" →
        # STATIC/GLOBAL.
        assert role.stability is PromptStability.STATIC
        assert role.cache_scope is PromptCacheScope.GLOBAL
        assert role.source == "core"

    def test_project_override_role_demotes_to_project_tier(
        self, tmp_path: Path,
    ) -> None:
        # ADR 0028 / M10.5: a project-level override of a role file
        # with no $vars must classify the part STATIC/PROJECT — body
        # bytes are stable while the pipeline targets this project,
        # invalidate when this project's override file changes.
        self._write(
            tmp_path,
            "roles/systems_architect",
            "You are the project-tuned architect.\n",
        )
        # Reset the prompt_loader cache so the new file is seen.
        from core.io import prompt_loader as _pl
        _pl.reload_cache()
        env = prompts.hypothesis_prompt("probe", str(tmp_path), codemap="x.py").envelope()
        role = next(p for p in env.parts if p.kind == "role")
        assert role.source == "project"
        assert role.stability is PromptStability.STATIC
        assert role.cache_scope is PromptCacheScope.PROJECT

    def test_workspace_override_role_demotes_to_workspace_tier(
        self, tmp_path: Path,
    ) -> None:
        # Build a workspace dir with the override and a separate
        # project subdir without one. The workspace auto-detection
        # walks up from project_dir; the prompts/ directory at the
        # workspace level signals workspace scope.
        workspace = tmp_path / "ws"
        project = workspace / "proj"
        project.mkdir(parents=True)
        # Mark workspace by placing the prompts subdir at its root.
        self._write(workspace, "roles/systems_architect", "ws role\n")
        from core.io import prompt_loader as _pl
        _pl.reload_cache()
        env = prompts.hypothesis_prompt("probe", str(project), codemap="x.py").envelope()
        role = next(p for p in env.parts if p.kind == "role")
        assert role.source == "workspace"
        assert role.stability is PromptStability.STATIC
        assert role.cache_scope is PromptCacheScope.WORKSPACE


# ---------------------------------------------------------------------------
# Step 4 — assemble_cache_first_segments.
# ---------------------------------------------------------------------------


class TestAssembleCacheFirstPrompt:
    def _part(
        self,
        kind: str,
        body: str,
        *,
        cache_scope: PromptCacheScope = PromptCacheScope.GLOBAL,
        stability: PromptStability = PromptStability.STATIC,
        name: str | None = None,
    ) -> object:
        from pipeline.prompts.types import PromptPart
        return PromptPart(
            kind=kind,
            name=name or kind,
            source="code-owned",
            body=body,
            stability=stability,
            cache_scope=cache_scope,
            volatile_reason=(
                "test fixture" if (
                    stability is not PromptStability.STATIC
                    or cache_scope is PromptCacheScope.NONE
                ) else None
            ),
        )

    def test_orders_by_cache_breadth_tier(self) -> None:
        from pipeline.prompts.composer import assemble_cache_first_segments
        parts = [
            self._part(
                "turn_input", "task body",
                cache_scope=PromptCacheScope.NONE,
                stability=PromptStability.TURN,
            ),
            self._part(
                "context", "project facts",
                cache_scope=PromptCacheScope.PROJECT,
            ),
            self._part("role", "static role"),
            self._part(
                "system_tail", "universal contract",
            ),
        ]
        turn = assemble_cache_first_segments(parts)
        kinds = [p.kind for p in turn.parts]
        # GLOBAL tier first (system_tail then role), then PROJECT
        # tier (context), then NONE tier (turn_input).
        assert kinds == ["system_tail", "role", "context", "turn_input"]
        assert turn.text.startswith("universal contract")
        assert turn.text.endswith("task body")

    def test_stable_within_tier(self) -> None:
        from pipeline.prompts.composer import assemble_cache_first_segments
        # Two parts that share (tier, kind): original order preserved.
        a = self._part("system_tail", "A", name="a")
        b = self._part("system_tail", "B", name="b")
        turn1 = assemble_cache_first_segments([a, b])
        assert [p.name for p in turn1.parts] == ["a", "b"]
        turn2 = assemble_cache_first_segments([b, a])
        assert [p.name for p in turn2.parts] == ["b", "a"]

    def test_uniform_separator(self) -> None:
        from pipeline.prompts.composer import assemble_cache_first_segments
        parts = [
            self._part("system_tail", "first"),
            self._part("role", "second"),
        ]
        turn = assemble_cache_first_segments(parts)
        assert turn.text == "first\n\nsecond"

    def test_empty_input_returns_empty(self) -> None:
        from pipeline.prompts.composer import assemble_cache_first_segments
        turn = assemble_cache_first_segments([])
        assert turn.text == ""
        assert turn.parts == ()

    def test_unknown_kind_falls_to_end_of_tier(self) -> None:
        from pipeline.prompts.composer import assemble_cache_first_segments
        parts = [
            self._part("custom_unlisted", "X"),
            self._part("role", "Y"),
        ]
        turn = assemble_cache_first_segments(parts)
        # role has a known kind index; custom_unlisted falls to the
        # end of its tier (GLOBAL).
        assert [p.kind for p in turn.parts] == ["role", "custom_unlisted"]


# ---------------------------------------------------------------------------
# Step 2 — typed dynamic parts replace $vars in task files.
# ---------------------------------------------------------------------------


_TASK_SENTINEL = "Fix 500 error on /api/users create — uniquely-sentinel-z42"


class TestTypedDynamicPartsEmission:
    """ADR 0028 / M10.5 Step 2: every primary builder now emits a
    typed ``turn_input`` part carrying the user task body instead of
    substituting ``$task`` into the task-method markdown file.
    """

    def test_hypothesis_emits_turn_input_with_user_task(self) -> None:
        env = prompts.hypothesis_prompt(_TASK_SENTINEL, "/proj", codemap="x.py").envelope()
        turn_inputs = [p for p in env.parts if p.kind == "turn_input"]
        assert len(turn_inputs) == 1
        assert turn_inputs[0].name == "hypothesis_task"
        assert _TASK_SENTINEL in turn_inputs[0].body
        # Task method part itself has no run-volatile content.
        task_method = next(p for p in env.parts if p.kind == "task")
        assert _TASK_SENTINEL not in task_method.body

    def test_plan_emits_turn_input_with_user_task(self) -> None:
        env = prompts.plan_prompt(_TASK_SENTINEL, "/proj", PluginConfig()).envelope()
        turn_inputs = {
            p.name: p for p in env.parts if p.kind == "turn_input"
        }
        assert "plan_task" in turn_inputs
        assert _TASK_SENTINEL in turn_inputs["plan_task"].body

    def test_replan_emits_turn_input_and_reviewer_critique(self) -> None:
        env = prompts.replan_prompt(
            _TASK_SENTINEL,
            "Critique: missing acceptance criteria.",
            "",
            "/proj",
            PluginConfig(),
        ).envelope()
        kinds_by_name = {
            p.name: p.kind for p in env.parts
            if p.kind in {"turn_input", "reviewer_critique", "human_feedback"}
        }
        assert kinds_by_name.get("replan_task") == "turn_input"
        assert (
            kinds_by_name.get("validate_plan_findings") == "reviewer_critique"
        )
        # operator feedback empty → no human_feedback part this round
        assert "operator_feedback" not in kinds_by_name

    def test_replan_with_human_feedback_emits_both_parts(self) -> None:
        env = prompts.replan_prompt(
            _TASK_SENTINEL,
            "Critique: missing acceptance criteria.",
            "Narrow scope to API only.",
            "/proj",
            PluginConfig(),
        ).envelope()
        by_name = {
            p.name: p
            for p in env.parts
            if p.kind in {"reviewer_critique", "human_feedback"}
        }
        rc = by_name.get("validate_plan_findings")
        hf = by_name.get("operator_feedback")
        assert rc is not None and rc.kind == "reviewer_critique"
        assert rc.source == "artifact"
        assert hf is not None and hf.kind == "human_feedback"
        assert hf.source == "operator"

    def test_fix_emits_turn_input_and_feedback(self) -> None:
        env = prompts.fix_prompt(
            _TASK_SENTINEL,
            "Critique: edge case missing.",
            "/proj",
            PluginConfig(),
            test_failures="",
        ).envelope()
        kinds_by_name = {
            p.name: p.kind for p in env.parts
            if p.kind in {"turn_input", "feedback"}
        }
        assert kinds_by_name.get("repair_task") == "turn_input"
        assert kinds_by_name.get("repair_body") == "feedback"

    def test_validate_plan_re_review_emits_receipt_and_current_subject(self) -> None:
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        parsed_plan = ParsedPlan(
            short_summary="Updated plan.",
            planning_context="Plan context.",
            subtasks=(SubTask(id="T1", goal="Fix the issue."),),
            source="json",
            goal="Goal",
            acceptance_criteria=("AC",),
        )
        env = prompts.plan_file_review_prompt(
            parsed_plan,
            _TASK_SENTINEL,
            PluginConfig(),
            "/proj",
            repair_receipt="F1 fixed in T1.",
            current_review_subject="Current plan subject.",
        ).envelope()
        parts = {p.id: p for p in env.parts}
        assert parts["repair_receipt:latest"].kind == "repair_receipt"
        assert parts["current_review_subject:latest"].kind == (
            "current_review_subject"
        )

    def test_runtime_re_review_emits_receipt_and_current_subject(self) -> None:
        env = prompts.runtime_review_uncommitted_prompt(
            "Review focus.",
            project_dir="/proj",
            repair_receipt="F1 fixed in file.py.",
            current_review_subject="git status output",
        ).envelope()
        parts = {p.id: p for p in env.parts}
        assert parts["repair_receipt:latest"].kind == "repair_receipt"
        assert parts["current_review_subject:latest"].kind == (
            "current_review_subject"
        )


class TestTaskAppearsExactlyOnce:
    """Guard A from the Step 2 brief: when a task file's ``$task``
    extraction lands the task into a typed ``turn_input`` part, the
    user instruction must appear in the wire prompt **exactly once**.
    Duplicate appearance means a leftover substitution leaked.
    """

    def _assert_single(self, rendered: str, needle: str) -> None:
        assert rendered.count(needle) == 1, (
            f"task sentinel {needle!r} appeared "
            f"{rendered.count(needle)} times in the rendered prompt"
        )

    def test_hypothesis_task_appears_once(self) -> None:
        rendered = prompts.hypothesis_prompt(
            _TASK_SENTINEL, "/proj", codemap="x.py",
        ).text
        self._assert_single(rendered, _TASK_SENTINEL)

    def test_plan_task_appears_once(self) -> None:
        rendered = prompts.plan_prompt(
            _TASK_SENTINEL, "/proj", PluginConfig(),
        ).text
        self._assert_single(rendered, _TASK_SENTINEL)

    def test_replan_task_appears_once(self) -> None:
        rendered = prompts.replan_prompt(
            _TASK_SENTINEL,
            "Critique body.",
            "",
            "/proj",
            PluginConfig(),
        ).text
        self._assert_single(rendered, _TASK_SENTINEL)

    def test_build_task_appears_once(self) -> None:
        rendered = prompts.build_prompt(
            _TASK_SENTINEL, "/proj", PluginConfig(),
        ).text
        self._assert_single(rendered, _TASK_SENTINEL)

    def test_review_focus_task_appears_once(self) -> None:
        rendered = prompts.review_focus(
            _TASK_SENTINEL, PluginConfig(), "/proj",
        ).text
        self._assert_single(rendered, _TASK_SENTINEL)


# ---------------------------------------------------------------------------
# Step 5 — physical wire order is cache-first.
# ---------------------------------------------------------------------------


_PROJECT_SENTINEL = "/work/api-vue-uniquely-sentinel-z42"


class TestWireOrderCacheFirst:
    """ADR 0028 / M10.5 Step 5: ``_render_prompt_output`` routes
    through ``assemble_cache_first_segments``. The physical wire bytes
    are therefore ordered by cache breadth — protected contracts and
    static composable parts (GLOBAL tier) lead, project context
    (PROJECT tier) follows, dynamic invocation parts (NONE tier)
    trail.
    """

    def test_envelope_text_starts_with_stable_prefix(self) -> None:
        # ``envelope.text`` must begin with the concatenated bodies of
        # ``stable_prefix_parts`` (joined by the renderer separator).
        # If the gateway ever drifts from the assembler this assertion
        # catches it immediately.
        env = prompts.plan_prompt(_TASK_SENTINEL, _PROJECT_SENTINEL, PluginConfig()).envelope()
        prefix_bodies = "\n\n".join(p.body for p in env.stable_prefix_parts)
        assert env.text.startswith(prefix_bodies)

    def test_first_dynamic_sentinel_appears_after_prefix_boundary(self) -> None:
        env = prompts.plan_prompt(_TASK_SENTINEL, _PROJECT_SENTINEL, PluginConfig()).envelope()
        prefix_len = sum(len(p.body) for p in env.stable_prefix_parts)
        # The user task sentinel is a TURN/NONE turn_input body —
        # it must appear only after the prefix boundary.
        task_idx = env.text.find(_TASK_SENTINEL)
        assert task_idx >= 0
        # +separator slack so the assertion is robust to the join.
        assert task_idx >= prefix_len

    def test_protected_contracts_precede_task_and_artifact(self) -> None:
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        # validate_plan ships review_json contract + typed plan
        # views (plan_contract:typed_plan, plan_tasks:execution_plan)
        # + user task — all in the same render. The cache-first
        # invariant is unchanged: protected contracts lead, dynamic
        # plan / task content trails.
        plan_body_sentinel = "Reviewed body sentinel z42."
        plan = ParsedPlan(
            short_summary="Stub plan.",
            planning_context="Stub.",
            subtasks=(SubTask(id="t1", goal=plan_body_sentinel),),
            source="json",
        )
        env = prompts.plan_file_review_prompt(
            plan,
            _TASK_SENTINEL,
            PluginConfig(),
            project_dir=_PROJECT_SENTINEL,
        ).envelope()
        contract_idx = env.text.find(
            '<orcho:system-block kind="contract" name="review_json"',
        )
        task_idx = env.text.find(_TASK_SENTINEL)
        # The plan body now travels in the plan_tasks part body.
        plan_idx = env.text.find(plan_body_sentinel)
        assert contract_idx >= 0
        assert task_idx >= 0
        assert plan_idx >= 0
        # Cache-first: protected contracts lead, dynamic content
        # (task body, plan body) trails.
        assert contract_idx < task_idx
        assert contract_idx < plan_idx

    def test_project_context_sits_after_global_and_before_dynamic(self) -> None:
        env = prompts.plan_prompt(_TASK_SENTINEL, _PROJECT_SENTINEL, PluginConfig()).envelope()
        # Walk envelope.parts; collect first index per tier.
        first_global = next(
            (i for i, p in enumerate(env.parts)
             if p.cache_scope is PromptCacheScope.GLOBAL),
            None,
        )
        first_project = next(
            (i for i, p in enumerate(env.parts)
             if p.cache_scope is PromptCacheScope.PROJECT),
            None,
        )
        first_none = next(
            (i for i, p in enumerate(env.parts)
             if p.cache_scope is PromptCacheScope.NONE),
            None,
        )
        assert first_global is not None
        assert first_project is not None
        assert first_none is not None
        assert first_global < first_project < first_none

    def test_envelope_parts_match_physical_wire_order_exactly(self) -> None:
        # The assembler output drives both ``text`` and ``parts``.
        # Concatenating ``part.body`` for parts in render order must
        # reproduce ``text`` (modulo trailing whitespace).
        env = prompts.plan_prompt(_TASK_SENTINEL, _PROJECT_SENTINEL, PluginConfig()).envelope()
        reconstructed = "\n\n".join(p.body for p in env.parts if p.body).strip()
        assert reconstructed == env.text

    def test_changing_task_text_does_not_shift_prefix_hash(self) -> None:
        env_a = prompts.plan_prompt("task A z42", _PROJECT_SENTINEL, PluginConfig()).envelope()

        env_b = prompts.plan_prompt("task B w99", _PROJECT_SENTINEL, PluginConfig()).envelope()

        # Same prefix bytes when only the task text changes (the task
        # rides in a TURN/NONE turn_input part outside the prefix).
        assert env_a.prefix_hash == env_b.prefix_hash

    def test_global_tier_bytes_invariant_across_project_change(self) -> None:
        # Two different project_dirs produce different PROJECT tier
        # bytes but the GLOBAL+WORKSPACE tiers are byte-identical.
        env_vue = prompts.plan_prompt(_TASK_SENTINEL, "/work/api-vue", PluginConfig()).envelope()

        env_php = prompts.plan_prompt(_TASK_SENTINEL, "/work/api-php", PluginConfig()).envelope()

        def _cacheable_global_workspace_bodies(env) -> str:
            return "\n\n".join(
                p.body for p in env.parts
                if p.cache_scope in {
                    PromptCacheScope.GLOBAL, PromptCacheScope.WORKSPACE,
                }
            )

        assert _cacheable_global_workspace_bodies(env_vue) == (
            _cacheable_global_workspace_bodies(env_php)
        )
        # PROJECT tier diverges by construction (different
        # project_dir).
        project_vue = "\n\n".join(
            p.body for p in env_vue.parts
            if p.cache_scope is PromptCacheScope.PROJECT
        )
        project_php = "\n\n".join(
            p.body for p in env_php.parts
            if p.cache_scope is PromptCacheScope.PROJECT
        )
        assert project_vue != project_php


# ---------------------------------------------------------------------------
# Step 3 — provenance labels are truthful.
# ---------------------------------------------------------------------------


class TestRuntimeArtifactsAreNotCodeOwned:
    """ADR 0028 / M10.5 Step 3: ``source="code-owned"`` means the body
    is a code/template constant. Runtime-generated bodies (typed plan,
    review critique, reviewed file content, cross-handoff state, repo
    map, hypothesis suffix) must use ``source="artifact"``. Protected
    contracts and minimal-intent fallbacks keep ``code-owned``.
    """

    def test_plan_contract_part_is_artifact(self) -> None:
        # Render an implement prompt that consumes a typed-plan
        # contract from a prior planning phase; the plan_contract
        # shadow PromptPart must be labelled ``artifact``.
        env = prompts.build_prompt(
            _TASK_SENTINEL,
            "/proj",
            PluginConfig(),
            plan_contract=(
                "## Plan Contract\n\n"
                "Acceptance: payload key matches v2 schema."
            ),
        ).envelope()
        plan = next(
            (p for p in env.parts if p.kind == "plan_contract"), None,
        )
        assert plan is not None
        assert plan.source == "artifact"

    def test_handoff_contract_part_is_artifact(self) -> None:
        env = prompts.build_prompt(
            _TASK_SENTINEL,
            "/proj",
            PluginConfig(),
            handoff_contract=(
                "## Cross Handoff\n\nproducer=api consumer=client"
            ),
        ).envelope()
        handoff = next(
            (p for p in env.parts if p.kind == "handoff_contract"), None,
        )
        assert handoff is not None
        assert handoff.source == "artifact"

    def test_file_validation_artifact_is_artifact(self) -> None:
        """validate_hypothesis still ships a kind=``artifact`` part
        (the reviewed hypothesis file). The renamed test focus: the
        artifact-class invariant — ``source="artifact"`` — survives
        the PR3 cutover that removed the plan-side artifact part.

        validate_plan no longer emits an ``artifact`` part on the
        normal path (PR3); see ``TestValidatePlanCompositionSplit``
        in test_dynamic_artifact_parts.py for the new typed-views
        coverage.
        """
        env = prompts.hypothesis_file_review_prompt(
            "/tmp/hypothesis.md",
            "# Hypothesis\n\nLikely payload mismatch.",
            _TASK_SENTINEL,
            project_dir="/proj",
        ).envelope()
        artifact = next(
            (p for p in env.parts if p.kind == "artifact"), None,
        )
        assert artifact is not None
        assert artifact.source == "artifact"

    def test_reviewer_critique_part_is_artifact(self) -> None:
        # replan emits reviewer_critique(validate_plan_findings) from
        # prior reviewer output; that body is by definition
        # runtime-generated.
        env = prompts.replan_prompt(
            _TASK_SENTINEL,
            "Critique: missing rollback path.",
            "",
            "/proj",
            PluginConfig(),
        ).envelope()
        rc_parts = [p for p in env.parts if p.kind == "reviewer_critique"]
        assert rc_parts, "expected reviewer_critique part"
        for rc in rc_parts:
            assert rc.source == "artifact"

    def test_human_feedback_part_is_operator(self) -> None:
        # phase_handoff_decide(retry_feedback) operator text rides as a
        # human_feedback part with source="operator".
        env = prompts.replan_prompt(
            _TASK_SENTINEL,
            "",
            "Stay inside the API; do not touch the SPA.",
            "/proj",
            PluginConfig(),
        ).envelope()
        hf_parts = [p for p in env.parts if p.kind == "human_feedback"]
        assert hf_parts, "expected human_feedback part"
        for hf in hf_parts:
            assert hf.source == "operator"

    def test_protected_contracts_remain_code_owned(self) -> None:
        # The system-tail protected contracts (plan_json,
        # change_handoff, authoring_language, etc.) are code/template
        # constants — their bodies are rendered from
        # :mod:`pipeline.prompts.contracts` factories. Must keep
        # ``code-owned`` after the Step 3 reclassification.
        env = prompts.plan_prompt(_TASK_SENTINEL, "/proj", PluginConfig()).envelope()
        system_tail = [p for p in env.parts if p.kind == "system_tail"]
        assert system_tail
        for st in system_tail:
            assert st.source == "code-owned"

    def test_turn_input_part_keeps_code_owned(self) -> None:
        # turn_input wraps user invocation parameters in a code-owned
        # framing string ("TASK:\n<user task>"). The user value is
        # runtime input but is NOT a prior-phase artifact; per the
        # Step 3 brief ("otherwise choose explicit existing
        # convention and document it") the part stays
        # ``code-owned`` — the convention is honest about the
        # framing's provenance and avoids conflating user input with
        # agent-generated output.
        env = prompts.plan_prompt(_TASK_SENTINEL, "/proj", PluginConfig()).envelope()
        turn_inputs = [p for p in env.parts if p.kind == "turn_input"]
        assert turn_inputs
        for ti in turn_inputs:
            assert ti.source == "code-owned"

    def test_context_project_part_keeps_code_owned(self) -> None:
        # project_context body composes project_dir + plugin config —
        # neither a code constant nor an agent-generated artifact.
        # The framing layout is code-owned; the convention is
        # documented in :func:`_project_context_part`.
        env = prompts.plan_prompt(_TASK_SENTINEL, "/proj", PluginConfig()).envelope()
        ctx = next(
            (p for p in env.parts if p.kind == "context"), None,
        )
        assert ctx is not None
        assert ctx.source == "code-owned"


# ---------------------------------------------------------------------------
# Step 1 — context part suppression (kept here for grouping).
# ---------------------------------------------------------------------------


def test_project_context_part_suppressed_when_no_facts() -> None:
    # hypothesis_review_focus accepts an optional project_dir; when
    # absent (and PluginConfig() yields an empty context block),
    # the project-context part must be suppressed entirely rather
    # than rendered as an empty block. An empty PromptPart would
    # confuse the envelope partitioner and pollute the trace.
    env = prompts.hypothesis_review_focus("probe", project_dir="").envelope()
    assert env is not None
    ctx_parts = [p for p in env.parts if p.kind == "context"]
    assert ctx_parts == []
