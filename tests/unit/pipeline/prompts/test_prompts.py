"""
Unit tests for prompts.py — pure string generation, no subprocess/filesystem
(except custom template file test which uses project_dir fixture).
"""

from pathlib import Path

import pytest

from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.contracts import (
    PromptEnvelope,
    SystemPromptBlock,
    change_handoff_strategy,
    compose_prompt,
    review_target_strategy,
)
from pipeline.runtime import PromptSpec


class TestPromptEnvelope:
    def test_plain_user_prompt_has_no_system_tail(self) -> None:
        assert compose_prompt("  user text  ") == "user text"

    def test_appends_generic_system_blocks_last(self) -> None:
        prompt = compose_prompt(
            "User prompt",
            system_tail=(
                SystemPromptBlock(name="output_json", body="Return JSON.", kind="format"),
                SystemPromptBlock(name="review_json", body="Return JSON."),
            ),
        )
        assert prompt.startswith("User prompt")
        assert '<orcho:system-block kind="format" name="output_json" version="1">' in prompt
        assert '<orcho:system-block kind="contract" name="review_json" version="1">' in prompt
        assert prompt.rstrip().endswith("</orcho:system-block>")

    def test_prompt_envelope_renders_same_join_point(self) -> None:
        block = SystemPromptBlock(name="x", body="system")
        env = PromptEnvelope("user", system_tail=(block,))
        assert env.render() == compose_prompt("user", system_tail=(block,))


class TestPromptTurnParts:
    """The builder gateway returns a :class:`PromptTurn` whose ``.parts``
    tuple carries the cache-first physical wire order. Wire output is
    unchanged — only the part-sequence contract is asserted here."""

    def test_hypothesis_prompt_stashes_upper_and_tail_seams(self) -> None:
        turn = prompts.hypothesis_prompt(
            "Fix calc.add", "/project", codemap="calc.py",
        )

        # turn.text is the wire-identical string passed to the agent.
        assert turn.text  # non-empty rendered prompt

        upper = [p for p in turn.parts if p.kind != "system_tail"]
        upper_kinds = [p.kind for p in upper]
        upper_names = [p.name for p in upper]
        # ADR 0028 / M10.5 Step 5: non-system_tail parts reflect the
        # cache-first physical wire order produced by
        # ``assemble_cache_first_segments``:
        #   - TIER GLOBAL: role → format → task method (default-core
        #     parts with no $vars)
        #   - TIER PROJECT: context:project
        #   - TIER NONE: turn_input (user task body)
        assert upper_kinds == [
            "role", "format", "task", "context", "turn_input",
        ]
        assert upper_names == [
            "systems_architect", "terse", "hypothesis",
            "project", "hypothesis_task",
        ]
        # Source labels per Step 3 provenance audit: editable
        # composable parts come from ``_prompts/`` (``core``);
        # context layout + turn_input framing stay ``code-owned``
        # (Step 3 keeps these two conventions documented at the
        # helper docstrings).
        for part in upper:
            if part.kind in {"context", "turn_input"}:
                assert part.source == "code-owned"
            else:
                assert part.source == "core"

        # Hypothesis pipeline attaches authoring_language as the only
        # system-tail block (engine default is English).
        tail = [p for p in turn.parts if p.kind == "system_tail"]
        tail_names = [p.name for p in tail]
        assert "authoring_language" in tail_names
        for part in tail:
            assert part.source == "code-owned"
            assert part.kind == "system_tail"


class TestHypothesisPrompts:
    def test_hypothesis_prompt_loads_template(self) -> None:
        # A follow-up: language directive moved out of the
        # user-editable tasks/hypothesis.md and now comes from the
        # ``authoring_language`` system-tail block. Engine default is
        # English; workspaces override via JSON / TASK_LANGUAGE env.
        p = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="calc.py").text
        assert "Fix calc.add" in p
        assert "calc.py" in p
        assert 'name="authoring_language"' in p
        assert "English" in p

    def test_hypothesis_review_focus_loads_template_and_attaches_contract(self) -> None:
        # REA-3.5 follow-up: hypothesis QA shares the typed JSON reviewer
        # contract — it's a lifecycle gate too, so the same system-owned
        # block enforces output shape. ADR 0028 / M10.5 Step 5: the
        # contract now leads the wire (TIER GLOBAL, kind sub-order
        # ``system_tail`` first) instead of trailing it.
        f = prompts.hypothesis_review_focus("Fix calc.add", project_dir="/project").text
        assert "Validate this implementation hypothesis" in f
        assert "Fix calc.add" in f
        assert '<orcho:system-block kind="contract" name="review_json" version="1">' in f
        assert "exactly one JSON object" in f
        assert "short_summary" in f
        # Protocol enums stay English.
        assert "APPROVED" in f
        assert "REJECTED" in f
        # Cache-first reorder: the contract block precedes the user
        # task / role / format bytes.
        assert f.find(
            '<orcho:system-block kind="contract" name="review_json"',
        ) < f.find("Fix calc.add")


class TestRuntimeProtocolPrompts:
    def test_readonly_plan_prompt_uses_template(self) -> None:
        # A follow-up: language directive moved out of the
        # user-editable tasks/readonly_plan.md and now comes from the
        # ``authoring_language`` system-tail block. Engine default is
        # English; workspaces override via JSON / TASK_LANGUAGE env.
        p = prompts.readonly_plan_prompt("Fix calc.add", "/project", codemap="calc.py").text
        assert "Fix calc.add" in p
        assert "calc.py" in p
        assert 'name="authoring_language"' in p
        assert "English" in p
        assert '<orcho:system-block kind="strategy" name="change_handoff" version="1">' in p

    def test_runtime_review_uncommitted_prompt_uses_template(self) -> None:
        p = prompts.runtime_review_uncommitted_prompt(
            "check for races",
            project_dir="/project",
        ).text
        assert "Review the configured code-change target" in p
        assert "check for races" in p
        assert '<orcho:system-block kind="strategy" name="review_target" version="1">' in p
        assert "Review target mode: uncommitted" in p

    def test_runtime_review_prompt_does_not_duplicate_existing_target_contract(self) -> None:
        focus = prompts.review_focus(
            "Fix calc.add",
            PluginConfig(),
            change_handoff="commit",
        ).text
        p = prompts.runtime_review_uncommitted_prompt(focus, project_dir="/project").text
        # Dedup invariant survives the wire reorder.
        assert p.count('name="review_target"') == 1
        assert p.count('name="review_json"') == 1
        assert "Review target mode: commit" in p
        # ADR 0028 / M10.5 Step 5: protected contracts now lead the
        # wire (TIER GLOBAL), so partitioning at the first
        # ``<orcho:system-block`` marker leaves an empty user_part —
        # which itself proves no user content sits before the
        # contract block. Assert presence in the post-marker remainder.
        _user_part, marker, system_part = p.partition("<orcho:system-block")
        assert marker
        assert 'name="review_target"' in system_part
        assert 'name="review_json"' in system_part

# ─────────────────────────────────────────────────────────────────────────────
# ADR 0025 — output_contract routing across review_focus +
# runtime_review_uncommitted_prompt. The kwarg is the only source of
# truth; no body-substring heuristics, no double-contract regressions.
# ─────────────────────────────────────────────────────────────────────────────


class TestOutputContractThreading:
    def test_review_focus_default_attaches_review_json_only(self) -> None:
        p = prompts.review_focus("Fix calc.add", PluginConfig()).text
        assert p.count('name="review_json"') == 1
        assert 'name="release_json"' not in p

    def test_review_focus_release_attaches_release_json_only(self) -> None:
        p = prompts.review_focus(
            "Final ship gate", PluginConfig(),
            output_contract="release",
        ).text
        assert p.count('name="release_json"') == 1
        assert 'name="review_json"' not in p

    def test_runtime_review_uncommitted_default_attaches_review_json_only(self) -> None:
        p = prompts.runtime_review_uncommitted_prompt(
            "check change", project_dir="/project",
        ).text
        assert p.count('name="review_json"') == 1
        assert 'name="release_json"' not in p

    def test_runtime_review_uncommitted_release_attaches_release_json_only(self) -> None:
        p = prompts.runtime_review_uncommitted_prompt(
            "check ship readiness", project_dir="/project",
            output_contract="release",
        ).text
        assert p.count('name="release_json"') == 1
        assert 'name="review_json"' not in p

    def test_wrapper_strip_tail_then_reattaches_release(self) -> None:
        """Critical invariant: ``runtime_review_uncommitted_prompt``
        strips any embedded system-tail block from focus and then
        re-attaches its OWN contract block based on ``output_contract``.
        Without this, a focus carrying ``release_json`` could lose its
        machine contract after strip-tail.
        """
        focus = prompts.review_focus(
            "Ship-readiness", PluginConfig(),
            output_contract="release",
        ).text
        # Focus already contains release_json — pass through the
        # wrapper with output_contract="release".
        p = prompts.runtime_review_uncommitted_prompt(
            focus, project_dir="/project",
            output_contract="release",
        ).text
        assert p.count('name="release_json"') == 1
        assert 'name="review_json"' not in p

    def test_wrapper_release_does_not_re_emit_review_json(self) -> None:
        """If a focus carries ``release_json`` and the caller picks
        ``output_contract="release"``, the wrapper must NOT auto-append
        ``review_json`` (double-contract bug class)."""
        focus = prompts.review_focus(
            "Ship-readiness", PluginConfig(),
            output_contract="release",
        ).text
        assert 'name="release_json"' in focus
        p = prompts.runtime_review_uncommitted_prompt(
            focus, project_dir="/project",
            output_contract="release",
        ).text
        # Exactly one release_json, zero review_json.
        assert p.count('name="release_json"') == 1
        assert 'name="review_json"' not in p

    def test_heuristic_resistance_release_with_review_substrings(self) -> None:
        """The explicit kwarg is the only source of truth. A prompt
        body that mentions both ``"final acceptance"`` and ``"review"``
        substrings must still produce exactly one release_json block
        when ``output_contract="release"``.
        """
        p = prompts.runtime_review_uncommitted_prompt(
            "Final acceptance review of the change focused on "
            "review_changes follow-up",
            project_dir="/project",
            output_contract="release",
        ).text
        assert p.count('name="release_json"') == 1
        assert 'name="review_json"' not in p

    def test_kwarg_wins_over_focus_body_when_caller_picks_review(self) -> None:
        """Inverse: if focus accidentally carries release_json but the
        caller picks ``output_contract="review"``, the wrapper strips
        the embedded contract (existing behaviour) and re-attaches
        ``review_json`` — the kwarg overrides body content."""
        focus = prompts.review_focus(
            "Some review task", PluginConfig(),
            output_contract="release",
        ).text
        p = prompts.runtime_review_uncommitted_prompt(
            focus, project_dir="/project",
            output_contract="review",
        ).text
        assert p.count('name="review_json"') == 1
        assert 'name="release_json"' not in p


class TestOperatorWaiverChannel:
    """``operator_waiver`` rides as a typed TURN part composed with the
    code-owned reconciliation policy. Empty string adds no part; the
    review gate stays JSON-only regardless."""

    def test_empty_waiver_adds_no_part(self) -> None:
        p = prompts.runtime_review_uncommitted_prompt(
            "check change", project_dir="/project",
        ).text
        assert 'name="operator_waiver"' not in p
        assert "OPERATOR WAIVER" not in p

    def test_non_empty_waiver_injects_reconciliation_and_body(self) -> None:
        p = prompts.runtime_review_uncommitted_prompt(
            "check change", project_dir="/project",
            operator_waiver="Operator verdict: legacy shim accepted.",
        ).text
        # Code-owned reconciliation policy is injected, not the bare body.
        assert "OPERATOR WAIVER" in p
        assert "MUST NOT reopen the waived" in p
        # Operator verdict body rides alongside the policy.
        assert "Operator verdict: legacy shim accepted." in p
        # Review gate stays JSON-only — exactly one machine contract.
        assert p.count('name="review_json"') == 1
        assert 'name="release_json"' not in p

    def test_waiver_threads_through_release_contract(self) -> None:
        p = prompts.runtime_review_uncommitted_prompt(
            "ship gate", project_dir="/project",
            output_contract="release",
            operator_waiver="Operator verdict: documented gap accepted.",
        ).text
        assert "OPERATOR WAIVER" in p
        assert "Operator verdict: documented gap accepted." in p
        # Release gate stays JSON-only.
        assert p.count('name="release_json"') == 1
        assert 'name="review_json"' not in p


class TestPlanPrompt:
    def test_contains_task(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text
        assert task in p

    def test_no_implementation_code(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text
        assert "implementation code" in p.lower()

    def test_injects_language(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text
        assert full_plugin.language in p

    def test_injects_architecture(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text
        assert full_plugin.architecture in p

    def test_injects_file_hints(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text
        for hint in full_plugin.file_hints:
            assert hint in p

    def test_appends_plan_extra(self, task: str) -> None:
        plugin = PluginConfig(plan_prompt_extra="Custom plan instruction XYZ")
        p = prompts.plan_prompt(task, "/project", plugin).text
        assert "Custom plan instruction XYZ" in p

    def test_assigns_verification_to_engine_gate_and_implement_tasks(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text

        assert "The engine runs the project's declared full or broad suite" in p
        assert "required\npost-implement gate" in p
        assert "implement tasks name only targeted tests" in p
        assert "not as an implement action" in p

    def test_appends_change_handoff_strategy(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(task, "/project", full_plugin).text
        assert '<orcho:system-block kind="strategy" name="change_handoff" version="1">' in p
        assert "Change handoff mode: uncommitted" in p
        assert "do not git add, commit" in p

    def test_commit_handoff_allows_task_commit(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.plan_prompt(
            task, "/project", full_plugin, change_handoff="commit",
        ).text
        assert "Change handoff mode: commit" in p
        assert "commit exactly the task-relevant changes" in p
        assert "Review target mode" not in p


class TestDecomposePlanPrompt:
    """The team-lead-style PLAN variant that emits a SubTask DAG."""

    def test_includes_task(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.decompose_plan_prompt(task, "/project", full_plugin).text
        assert task in p

    def test_emits_json_schema_doc(self, task: str) -> None:
        p = prompts.decompose_plan_prompt(task, "/project", PluginConfig()).text
        # Schema doc anchor strings (defined in core/contracts/plan_schema.py).
        #  machine schema moved into the ``plan_json`` system-tail
        # contract, so the assertions hit the rendered output regardless
        # of source layer.
        assert "short_summary" in p
        assert "planning_context" in p
        assert "depends_on" in p
        assert "exactly one JSON object" in p
        # Fenced-code-block prohibition is now phrased by
        # ``plan_json_contract``: "Do not put the object inside a
        # ```json``` (or any other) fenced code block."
        assert "markdown fence" in p
        # The plan_json system-block anchors the contract.
        assert '<orcho:system-block kind="contract" name="plan_json"' in p

    def test_skill_roster_listed_when_present(self, task: str) -> None:
        from pipeline.skills import SkillPackage

        plugin = PluginConfig()
        plugin.skill_registry = {
            "backend": SkillPackage(
                name="backend",
                description="Adds REST endpoints.",
                root_dir=Path("/tmp/skills/backend"),
                skill_md_path=Path("/tmp/skills/backend/SKILL.md"),
                body="",
                frontmatter={
                    "name": "backend",
                    "description": "Adds REST endpoints.",
                },
                source="project",
                checksum="sha256:backend",
            ),
        }
        p = prompts.decompose_plan_prompt(task, "/project", plugin).text
        assert "AVAILABLE SKILLS" in p
        assert "`backend`" in p
        assert "Adds REST endpoints" in p

    def test_empty_roster_message_when_no_skills(self, task: str) -> None:
        p = prompts.decompose_plan_prompt(task, "/project", PluginConfig()).text
        # No skills registered → guidance to omit the `skill` field.
        assert "AVAILABLE SKILLS" in p
        assert "none registered" in p

    def test_includes_task_section_format(self, task: str) -> None:
        p = prompts.decompose_plan_prompt(task, "/project", PluginConfig()).text
        # The format the parser expects.
        assert '"tasks"' in p
        assert '"depends_on"' in p
        assert '"done_criteria"' in p

    def test_no_implementation_code_yet(self, task: str) -> None:
        p = prompts.decompose_plan_prompt(task, "/project", PluginConfig()).text
        assert "implementation code" in p.lower()

    def test_custom_template_file(self, task: str, project_dir: str) -> None:
        tmpl = Path(project_dir) / "custom_plan.md"
        tmpl.write_text("PROJECT TEMPLATE: {task}")
        plugin = PluginConfig(custom_plan_prompt_file="custom_plan.md")
        p = prompts.plan_prompt(task, project_dir, plugin).text
        assert task in p
        assert "PROJECT TEMPLATE" in p

    def test_default_plugin_works(self, task: str, default_plugin: PluginConfig) -> None:
        """Must not raise with empty plugin (generic mode)."""
        p = prompts.plan_prompt(task, "/project", default_plugin).text
        assert task in p

    def test_default_plan_prompt_requires_machine_contract(
        self, task: str, default_plugin: PluginConfig,
    ) -> None:
        #  machine contract now lives in the ``plan_json``
        # system-tail block (``pipeline.prompts.contracts``). The
        # rendered prompt must still expose the schema directives to
        # the architect agent, but they come from system-tail now.
        p = prompts.plan_prompt(task, "/project", default_plugin).text
        assert '<orcho:system-block kind="contract" name="plan_json"' in p
        assert "exactly one JSON object" in p
        assert "markdown fence" in p
        assert '"short_summary"' in p
        assert '"planning_context"' in p
        assert '"acceptance_criteria"' in p
        # Run-artifact persistence is code-owned orchestration policy,
        # not user-editable PLAN task prose.
        assert 'name="plan_artifact_boundary"' in p
        assert "Do not call Write or Edit" in p


class TestBuildPrompt:
    def test_contains_task(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.build_prompt(task, "/project", full_plugin).text
        assert task in p

    def test_references_docs_dir(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.build_prompt(task, "/project", full_plugin).text
        assert full_plugin.ma_artifacts_dir in p

    def test_appends_build_extra(self, task: str) -> None:
        plugin = PluginConfig(build_prompt_extra="Never touch .meta files")
        p = prompts.build_prompt(task, "/project", plugin).text
        assert "Never touch .meta files" in p

    def test_appends_change_handoff_strategy(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.build_prompt(task, "/project", full_plugin).text
        assert '<orcho:system-block kind="strategy" name="change_handoff" version="1">' in p
        assert "Leave code/test changes in the working tree" in p
        assert "Do not run destructive git commands" in p
        assert "git checkout -- <path>" in p

    def test_build_prompt_preserves_user_owned_changes(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        # ADR 0009 the user-owned-changes policy now comes
        # exclusively from the ``change_handoff`` system-tail block,
        # not from a user-editable part.
        p = prompts.build_prompt(task, "/project", full_plugin).text
        assert "pre-existing uncommitted changes as user-owned" in p
        assert "git restore" in p


class TestBuildPromptAdvisoryCritique:
    """``advisory_critique`` forwards bypassed plan-review findings into the
    implement turn as a typed ``reviewer_critique`` part wrapped in the
    code-owned advisory policy. Empty string adds no part."""

    _FINDINGS = "FINDING: acceptance criteria are too vague to verify."

    def test_advisory_critique_emits_reviewer_critique_part(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        turn = prompts.build_prompt(
            task, "/project", full_plugin, advisory_critique=self._FINDINGS,
        )
        rc = [p for p in turn.parts if p.kind == "reviewer_critique"]
        assert len(rc) == 1
        # Same typed part the replan path reuses — no new kind/id.
        assert rc[0].id == "reviewer_critique:validate_plan_findings"
        # Original findings text rides verbatim.
        assert self._FINDINGS in rc[0].body
        # Code-owned advisory framing keeps the findings advisory, not a
        # replan command or a blocking gate.
        assert "Do not replan" in rc[0].body
        assert "reviewer advisory feedback" in rc[0].body
        assert "Address applicable findings while implementing" in rc[0].body
        assert "implementation handoff" in rc[0].body
        # Findings + framing reach the wire.
        assert self._FINDINGS in turn.text
        assert "Do not replan" in turn.text

    def test_advisory_critique_present_in_minimal_mode(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        # Mode-independent: the part survives the minimal ablation path
        # where the composed extra_parts would otherwise be empty.
        turn = prompts.build_prompt(
            task, "/project", full_plugin,
            advisory_critique=self._FINDINGS,
            professional_prompt_mode="minimal",
        )
        rc = [p for p in turn.parts if p.kind == "reviewer_critique"]
        assert len(rc) == 1
        assert self._FINDINGS in rc[0].body
        assert "Do not replan" in rc[0].body

    def test_empty_advisory_critique_adds_no_part(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        turn = prompts.build_prompt(
            task, "/project", full_plugin, advisory_critique="",
        )
        assert not [p for p in turn.parts if p.kind == "reviewer_critique"]
        assert "Do not replan" not in turn.text
        assert "reviewer advisory feedback" not in turn.text


class TestReviewFocus:
    def test_contains_task(self, task: str, full_plugin: PluginConfig) -> None:
        f = prompts.review_focus(task, full_plugin).text
        assert task[:80] in f

    def test_uses_composed_reviewer_parts(self, task: str) -> None:
        f = prompts.review_focus(task, PluginConfig()).text
        assert "You are the application architect for this task." in f
        # ADR 0028 / M10.5 Step 2: task method opens with the static
        # "Review the code changes against the task." line; the user
        # task itself rides in the typed turn_input part appended
        # later in the wire.
        assert "Review the code changes against the task" in f
        assert task in f
        # Findings-discipline anchor (P1b rewrite — method-first phrasing,
        # but the underlying contract — only findings that matter for
        # correctness/completeness/maintainability/task contract — survives).
        assert "Report only findings that affect correctness" in f


    def test_appends_review_extra(self, task: str) -> None:
        plugin = PluginConfig(review_focus_extra="Check for N+1 queries")
        f = prompts.review_focus(task, plugin).text
        assert "N+1" in f

    def test_review_json_contract_always_attached(self, task: str, full_plugin: PluginConfig) -> None:
        # REA-3.5: every reviewer call (REVIEW + FINAL_ACCEPTANCE) emits the typed
        # JSON contract. ``require_verdict`` is preserved on the API but no
        # longer changes the output shape. ADR 0028 / M10.5 Step 5: the
        # contract block now leads the wire (cache-first); presence
        # (anywhere in the rendered prompt) is the invariant.
        plain = prompts.review_focus(task, full_plugin).text
        gated = prompts.review_focus(task, full_plugin, require_verdict=True).text
        review_block = '<orcho:system-block kind="contract" name="review_json" version="1">'
        assert review_block in plain
        assert review_block in gated
        assert '<orcho:system-block kind="strategy" name="review_target" version="1">' in plain
        assert '<orcho:system-block kind="strategy" name="review_target" version="1">' in gated
        # ADR 0009 reviewer safety policy moved out of the
        # tasks/code_review.md user-template into the
        # ``review_target_strategy`` system-tail block.
        assert (
            "Do not recommend removing or reverting pre-existing user-owned changes"
            in plain
        )
        # Schema directives are system-owned, embedded in the contract block.
        assert "short_summary" in plain
        assert "exactly one JSON object" in plain

    def test_commit_set_handoff_selects_commit_set_review_target(self, task: str) -> None:
        f = prompts.review_focus(
            task,
            PluginConfig(),
            change_handoff="commit_set",
        ).text
        assert "Review target mode: commit_set" in f
        assert "working tree diff for tracked files" not in f

    def test_explicit_prompt_role_matches_default(self, task: str) -> None:
        """A5.2a: a profile-supplied spec carrying the builder's default
 prompt role explicitly produces the same output as the
 no-spec call (which uses that default internally). Pins the
 invariant that profile authors get the same render by either
 omitting the spec or naming the prompt-taxonomy role
 explicitly."""
        default = prompts.review_focus(task, PluginConfig()).text
        via_step = prompts.review_focus(
            task,
            PluginConfig(),
            prompt_spec=PromptSpec(
                role="code_reviewer", task="code_review", format="detailed",
            ),
        ).text
        assert via_step == default

    def test_explicit_prompt_role_overrides_default(
        self, task: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A profile may set ``prompt.role`` to a non-default persona
 (e.g. ``technical_editor``). The explicit role threads through
 rendering verbatim — no runtime-role fallback is consulted."""
        from core.io import prompt_loader

        seen: list[str] = []
        real = prompt_loader.render_prompt

        def spy(name, project_dir=None, **kwargs):
            seen.append(name)
            # Pretend the alt persona file exists by aliasing it to the
            # default code_reviewer template — we only assert on the lookup name.
            if name == "roles/technical_editor":
                return real("roles/code_reviewer", project_dir=project_dir, **kwargs)
            return real(name, project_dir=project_dir, **kwargs)

        monkeypatch.setattr("pipeline.prompts.composer.prompt_loader.render_prompt", spy)

        prompts.review_focus(
            task,
            PluginConfig(),
            prompt_spec=PromptSpec(task="code_review", role="technical_editor"),
        )
        assert "roles/technical_editor" in seen
        # Neither the runtime role name nor the builder's default prompt
        # role should be looked up when an explicit override is supplied.
        assert "roles/reviewer" not in seen
        assert "roles/code_reviewer" not in seen



class TestPlanReviewFocus:
    def test_no_style_nitpick(self, task: str, full_plugin: PluginConfig) -> None:
        f = prompts.plan_review_focus(task, full_plugin).text
        assert "Do NOT nitpick style" in f

    def test_targets_plan_document(self, task: str, full_plugin: PluginConfig) -> None:
        f = prompts.plan_review_focus(task, full_plugin).text
        assert "PLAN document" in f

    def test_contains_task(self, task: str, full_plugin: PluginConfig) -> None:
        f = prompts.plan_review_focus(task, full_plugin).text
        assert task in f

    def test_review_json_contract_leads_cacheable_prefix(self, task: str, full_plugin: PluginConfig) -> None:
        # ADR 0028 / M10.5 Step 5: the reviewer JSON contract now
        # leads the wire (TIER GLOBAL, ``system_tail`` kind sub-order
        # 0). Pre-Step-5 this test asserted the contract trailed
        # (``rstrip().endswith("</orcho:system-block>")``).
        f = prompts.plan_review_focus(task, full_plugin).text
        assert '<orcho:system-block kind="contract" name="review_json" version="1">' in f
        assert "exactly one JSON object" in f
        assert "short_summary" in f
        # Protocol enum values stay English exactly as listed in the schema.
        assert "APPROVED" in f
        assert "REJECTED" in f
        # The contract precedes the user task text in the wire bytes.
        assert f.find(
            '<orcho:system-block kind="contract" name="review_json"',
        ) < f.find(task)

    def test_user_editable_template_does_not_carry_schema(self) -> None:
        # The reviewer task body ships the review focus only — schema/JSON
        # rules come from the system-owned ``review_json`` contract. This
        # protects the parser from project-level template edits silently
        # breaking output shape.
        from core.io.prompt_loader import render_prompt

        template = render_prompt(
            "tasks/validate_plan", project_dir=None, task="x", extra_checks="",
        )
        for token in ("short_summary", "REVIEW_SCHEMA_DOC", "JSON object"):
            assert token not in template


class TestReplanPrompt:
    def test_contains_critique(self, task: str, full_plugin: PluginConfig) -> None:
        critique = "Missing error handling for invalid input"
        p = prompts.replan_prompt(task, critique, "", "/project", full_plugin).text
        assert critique in p

    def test_no_implementation_code(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.replan_prompt(task, "some critique", "", "/project", full_plugin).text
        assert "implementation code" in p.lower()

    def test_contains_task(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.replan_prompt(task, "critique", "", "/project", full_plugin).text
        assert task in p

    def test_appends_change_handoff_strategy(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.replan_prompt(task, "critique", "", "/project", full_plugin).text
        assert '<orcho:system-block kind="strategy" name="change_handoff" version="1">' in p

    def test_human_feedback_alone_renders(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        # Operator-driven retry with no prior reviewer rejection still
        # produces a coherent replan prompt and never wraps the operator
        # text in any "Reviewer found these issues" framing.
        feedback = "Stay inside the API; do not touch the SPA layer."
        p = prompts.replan_prompt(task, "", feedback, "/project", full_plugin).text
        assert task in p
        assert feedback in p
        assert "Reviewer found these issues" not in p
        # No reviewer critique body anywhere in this render.
        assert "validate_plan_findings" not in p or feedback not in (
            p.split("validate_plan_findings", 1)[1]
        )

    def test_both_sources_render_distinct_bodies(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        critique = "Plan lacks rollback strategy."
        feedback = "Scope to migrations only; defer the dashboard."
        p = prompts.replan_prompt(
            task, critique, feedback, "/project", full_plugin,
        ).text
        assert critique in p
        assert feedback in p
        # Reviewer critique must not be wrapped by operator-feedback
        # framing and vice versa.
        assert "Reviewer found these issues" not in p

    def test_no_reviewer_framing_around_operator_text(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        feedback = "Narrow scope strictly to the API layer."
        p = prompts.replan_prompt(task, "", feedback, "/project", full_plugin).text
        assert "Reviewer found these issues" not in p


class TestFixPrompt:
    def test_contains_critique(self, task: str, full_plugin: PluginConfig) -> None:
        critique = "Logic error in line 42"
        p = prompts.fix_prompt(task, critique, "/project", full_plugin).text
        assert critique in p

    def test_instructs_to_fix_all(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.fix_prompt(task, "some issue", "/project", full_plugin).text
        assert "please fix all issues" in p.lower()

    def test_contains_task(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.fix_prompt(task, "critique", "/project", full_plugin).text
        assert task in p

    def test_appends_change_handoff_strategy(self, task: str, full_plugin: PluginConfig) -> None:
        p = prompts.fix_prompt(task, "critique", "/project", full_plugin).text
        assert '<orcho:system-block kind="strategy" name="change_handoff" version="1">' in p
        assert "Do not run destructive git commands" in p

    def test_fix_prompt_preserves_user_owned_changes(
        self, task: str, full_plugin: PluginConfig,
    ) -> None:
        # ADR 0009 the user-owned-changes policy now comes
        # exclusively from the ``change_handoff`` system-tail block,
        # not from a user-editable part.
        p = prompts.fix_prompt(task, "critique", "/project", full_plugin).text
        assert "pre-existing uncommitted changes as user-owned" in p
        assert "git checkout -- <path>" in p


class TestPromptContracts:
    def test_change_handoff_modes(self) -> None:
        assert "Change handoff mode: uncommitted" in change_handoff_strategy(
            mode="uncommitted",
        ).render()
        assert "git checkout -- <path>" in change_handoff_strategy(
            mode="uncommitted",
        ).render()
        assert "pre-existing uncommitted changes as user-owned" in change_handoff_strategy(
            mode="uncommitted",
        ).render()
        assert "Change handoff mode: commit" in change_handoff_strategy(
            mode="commit",
        ).render()
        assert "Change handoff mode: commit_set" in change_handoff_strategy(
            mode="commit_set",
        ).render()

    def test_review_target_modes(self) -> None:
        assert "Review target mode: uncommitted" in review_target_strategy(
            mode="uncommitted",
        ).render()
        assert "Review target mode: commit" in review_target_strategy(
            mode="commit",
        ).render()
        assert "Review target mode: commit_set" in review_target_strategy(
            mode="commit_set",
        ).render()

    def test_unknown_change_handoff_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported change handoff"):
            change_handoff_strategy(mode="branch")


class TestDeveloperFamilyMigration:
    """ADR 0009 BUILD / FIX render through composed parts.

 Pins:
 default rendering uses ``PromptSpec(task=..., format="handoff")``
 and is equivalent to passing the same spec explicitly;
 composed output preserves the critical content guards
 (destructive-git rules, user-owned changes policy, FIX
 test-failure body, change_handoff system-tail);
 no literal ``$var`` placeholders leak into the rendered prompt;
 legacy flat overrides ``developer_build.md`` / ``developer_fix.md``
 still win when present in a project directory.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add multi-stage Dockerfile to a Python service."

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    @pytest.fixture
    def critique(self) -> str:
        return "Healthcheck endpoint is missing."

    # ── BUILD ─────────────────────────────────────────────────────────────

    def test_build_default_matches_explicit_spec(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        from pipeline.runtime import PromptSpec
        baseline = prompts.build_prompt(task, "/project", plugin).text
        explicit = prompts.build_prompt(
            task, "/project", plugin,
            prompt_spec=PromptSpec(
                role="implementation_engineer", task="implement", format="handoff",
            ),
        ).text
        assert baseline == explicit

    def test_build_no_placeholder_leaks(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.build_prompt(task, "/project", plugin).text
        for var in (
            "$task", "$body", "$extra_step", "$ma_artifacts_dir",
            "$task_language", "$context", "$project_dir",
        ):
            assert var not in out, f"literal {var} leaked into BUILD render"

    def test_build_preserves_critical_content(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.build_prompt(task, "/project", plugin).text
        assert task in out
        # Phase Q1a: role file renamed to ``implementation_engineer.md``.
        # Phase Q2 P0 rewrite established the professional posture anchor.
        assert "implementation engineer" in out.lower()
        # Phase P1a tasks/build.md professional method anchor.
        assert "smallest coherent implementation path" in out
        # Authoring safety policy now exclusively from system-tail
        # (`change_handoff_strategy`). Anchor on the two phrases that
        # the boundary guard pins.
        assert "destructive git" in out
        assert "pre-existing uncommitted changes as user-owned" in out
        # System-tail change_handoff still appended.
        assert 'name="change_handoff"' in out

    # ── FIX ───────────────────────────────────────────────────────────────

    def test_fix_default_matches_explicit_spec(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        from pipeline.runtime import PromptSpec
        baseline = prompts.fix_prompt(task, critique, "/project", plugin).text
        explicit = prompts.fix_prompt(
            task, critique, "/project", plugin,
            prompt_spec=PromptSpec(
                role="implementation_engineer", task="repair_changes", format="handoff",
            ),
        ).text
        assert baseline == explicit

    def test_fix_no_placeholder_leaks(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        out = prompts.fix_prompt(
            task, critique, "/project", plugin,
            test_failures="test_health failed",
        ).text
        for var in (
            "$task", "$body", "$extra_step", "$ma_artifacts_dir",
            "$task_language", "$context", "$project_dir",
        ):
            assert var not in out, f"literal {var} leaked into FIX render"

    def test_fix_preserves_test_failure_and_critique(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        out = prompts.fix_prompt(
            task, critique, "/project", plugin,
            test_failures="test_health failed",
        ).text
        # $body insertion point survived the split: critique and test
        # failures must reach the agent.
        assert critique in out
        assert "test_health failed" in out
        assert "test suite is FAILING" in out
        # Minimal-diff / edit-scope discipline lives in tasks/fix.md
        # (task-side instruction; not parser contract, not generic
        # style — replaces the role-bound formats/code_changes.md).
        # Phase Q2 P0 rewrite restated the rule mid-sentence; case-insensitive.
        assert "leave unrelated file content unchanged" in out.lower()
        # Authoring safety policy now exclusively from system-tail.
        assert "destructive git commands" in out
        assert 'name="change_handoff"' in out

    # ── Legacy override path ──────────────────────────────────────────────



    def test_safety_only_from_system_tail_for_build(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """After the destructive-git rules and user-owned-changes
 preservation policy come exclusively from the ``change_handoff``
 system-tail block — never from user-editable parts.

 Boundary check kept minimal and stable: pin only the two phrase
 anchors that are guaranteed by ``change_handoff_strategy`` in
 uncommitted mode. Other wording can change in that block without
 breaking this test.
 """
        out = prompts.build_prompt(task, "/project", plugin).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker, (
            "system-tail boundary marker not found in BUILD prompt — "
            "either change_handoff stopped being appended or the block "
            "tag changed shape"
        )
        assert "destructive git" not in user_part, (
            "destructive-git policy leaked back into the user-editable "
            "portion of BUILD prompt"
        )
        assert "destructive git" in system_part
        assert "pre-existing uncommitted changes as user-owned" in system_part

    def test_safety_only_from_system_tail_for_fix(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        out = prompts.fix_prompt(task, critique, "/project", plugin).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker, (
            "system-tail boundary marker not found in FIX prompt — "
            "either change_handoff stopped being appended or the block "
            "tag changed shape"
        )
        assert "destructive git" not in user_part, (
            "destructive-git policy leaked back into the user-editable "
            "portion of FIX prompt"
        )
        assert "destructive git" in system_part
        assert "pre-existing uncommitted changes as user-owned" in system_part

    # ── Boundary guard: authoring language policy lives only in system-tail ─

    def test_authoring_language_only_from_system_tail_for_build(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """ADR 0009 the language directive is config-owned policy
        and must come exclusively from the ``authoring_language``
        system-tail block — never from a user-editable role/task/format
        part. A project override that drops the directive must not be
        able to silently switch the agent to English output.
        """
        out = prompts.build_prompt(task, "/project", plugin).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker
        # User-editable portion must not carry the language directive
        # in any of its known phrasings.
        assert "Write natural-language output" not in user_part, (
            "language directive leaked back into the user-editable "
            "portion of BUILD prompt"
        )
        # System-tail must carry the directive via authoring_language.
        assert 'name="authoring_language"' in system_part
        assert "Write natural-language output" in system_part
        assert "code comments" not in system_part
        assert "docstrings" not in system_part
        assert "inline documentation" not in system_part

    def test_authoring_language_only_from_system_tail_for_fix(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        out = prompts.fix_prompt(task, critique, "/project", plugin).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker
        assert "Write natural-language output" not in user_part, (
            "language directive leaked back into the user-editable "
            "portion of FIX prompt"
        )
        assert 'name="authoring_language"' in system_part
        assert "Write natural-language output" in system_part
        assert "code comments" not in system_part
        assert "docstrings" not in system_part
        assert "inline documentation" not in system_part

    def test_authoring_language_strategy_returns_none_for_empty_config(
        self,
    ) -> None:
        """``authoring_language_strategy`` returns ``None`` when
 ``task_language`` is empty or whitespace-only so an empty
 config produces no block at all (no trivial "use the default
 language" injection). The conditional helper filters it out of
 the system-tail tuple via ``_render_prompt_output``'s ``None``
 filter.
 """
        from pipeline.prompts.contracts import authoring_language_strategy
        assert authoring_language_strategy(task_language=None) is None
        assert authoring_language_strategy(task_language="") is None
        assert authoring_language_strategy(task_language="   ") is None
        block = authoring_language_strategy(task_language="Russian")
        assert block is not None
        assert block.name == "authoring_language"
        assert "Russian" in block.body


class TestValidatePlanFamilyMigration:
    """validate_plan renders through composed parts (ADR 0009).

 Pins:
 default rendering uses
 ``PromptSpec(task="validate_plan")``
 and is equivalent to passing the same spec explicitly;
 JSON output shape stays code-owned via
 ``review_json_contract`` system-tail (project overrides of the
 user-editable role/task parts cannot drop the parser contract);
 the user-editable parts do NOT carry ``VERDICT:`` legacy
 lines or any JSON schema body — those belong to the
 ``review_json`` system-tail block.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add pagination to the user listing endpoint."

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_default_matches_explicit_spec(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        baseline = prompts.plan_review_focus(task, plugin, "/project").text
        explicit = prompts.plan_review_focus(
            task, plugin, "/project",
            prompt_spec=PromptSpec(
                role="plan_reviewer", task="validate_plan", format="detailed",
            ),
        ).text
        assert baseline == explicit

    def test_no_placeholder_leaks(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_review_focus(task, plugin, "/project").text
        for var in ("$task", "$extra_checks", "$task_language", "$context"):
            assert var not in out, (
                f"literal {var} leaked into validate_plan render"
            )

    def test_preserves_review_intent(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_review_focus(task, plugin, "/project").text
        # Solution-architecture review is distinct from code review:
        # no implementation diff exists yet.
        assert "You are the solution architecture reviewer for this task." in out
        assert "implementation diff yet" in out
        assert "sub-agents" in out
        assert "delegation boundaries" in out
        # PLAN-specific review procedure intact.
        assert "This is a PLAN document (no code yet)" in out
        assert "Focus on correctness and completeness of the plan." in out
        # Task interpolated.
        assert task in out

    def test_review_json_contract_in_system_tail(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_review_focus(task, plugin, "/project").text
        # The JSON contract block must be appended as the system-tail
        # and carry the canonical name + shape directives.
        assert '<orcho:system-block kind="contract" name="review_json"' in out
        assert "exactly one JSON object" in out
        assert "APPROVED" in out
        assert "REJECTED" in out

    def test_user_part_carries_no_json_schema_body(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """Parser-critical schema directives live in the system-tail
 block, never in the user-editable parts. Project overrides
 of ``roles/plan_reviewer`` or ``tasks/validate_plan`` must not be able
 to silently drop the JSON contract.
 """
        out = prompts.plan_review_focus(task, plugin, "/project").text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker, (
            "review_json system-tail boundary not found in validate_plan render"
        )
        # User template MUST NOT carry the JSON schema enum values or
        # shape constraints — those live in review_json_contract.
        assert "system-tail" not in user_part
        assert "review_json" not in user_part
        assert "JSON" not in user_part
        assert "schema" not in user_part
        assert "exactly one JSON object" not in user_part
        assert '"verdict":' not in user_part
        assert "P0" not in user_part
        # Conversely, system-tail MUST carry them.
        assert "exactly one JSON object" in system_part
        assert "APPROVED" in system_part
        assert "REJECTED" in system_part



class TestFinalAcceptanceFamilyMigration:
    """final_acceptance renders through the release-manager persona.

 The builder is shared with review_changes, so this test pins the
 profile-level prompt spec that gives the final gate its separate
 release-readiness voice.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add resumable cross-project contract checks."

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_release_manager_spec_renders_final_acceptance_parts(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus(
            task,
            plugin,
            "/project",
            prompt_spec=PromptSpec(
                role="release_manager",
                task="final_acceptance",
                format="bullets",
            ),
        ).text
        assert "You are the release manager for this task." in out
        assert "Is this change ready to ship as-is?" in out
        # Release-blocker posture must survive the M10 prose rewrite.
        # The role copy now says "concrete blockers" (without the
        # implicit "release" word the pre-M10 prose carried).
        assert "concrete blockers" in out
        assert task in out

    def test_verification_gaps_defer_to_declared_readiness_schedule(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """The release reviewer must not be a second owner of "what blocks
        ship" (ADR 0114 single-source). The final_acceptance task prompt
        scopes verification gaps to the declared verification readiness
        summary — a required check it lists under "Remaining before ready" —
        and explicitly classifies shipping-allowed / manual-only checks as
        non-blocking, so a genuinely green run is not falsely rejected by a
        gap the schedule never declared.

        Pins the reconciliation in code: a future edit that restores the old
        unconditional "an unaddressed gap blocks ship" mandate (independent of
        the declared schedule) fails here.
        """
        out = prompts.review_focus(
            task,
            plugin,
            "/project",
            prompt_spec=PromptSpec(
                role="release_manager",
                task="final_acceptance",
                format="bullets",
            ),
        ).text
        # Authoritative source of required proof is the readiness summary,
        # keyed to its own "Remaining before ready" verdict.
        assert "readiness summary" in out
        assert "Remaining before ready" in out
        # Non-blocking classes are named explicitly so the reviewer does not
        # invent a gap for proof the schedule does not require.
        assert "shipping allowed by policy" in out
        assert "manual-only" in out
        assert "not a gap" in out
        # The deliberate channel rule survives: transcript commands are not
        # accepted as proof; the closing step is to capture a receipt.
        assert "not proof" in out
        # The old unconditional mandate must be gone.
        assert "An unaddressed gap blocks ship." not in out

    def test_review_json_contract_stays_code_owned(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus(
            task,
            plugin,
            "/project",
            prompt_spec=PromptSpec(
                role="release_manager",
                task="final_acceptance",
                format="bullets",
            ),
        ).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker
        assert "exactly one JSON object" not in user_part
        assert 'name="review_json"' in system_part
        assert "exactly one JSON object" in system_part



class TestPlanSchemaContractBoundary:
    """ADR 0009 PLAN_SCHEMA_DOC moved out of user-editable
 architect_plan / architect_decompose / architect_replan templates
 into the ``plan_json`` system-tail contract.

 Pins:
 schema body, enum-shaped fields, and "exactly one JSON object"
 appear ONLY in the system-tail portion of the rendered prompt;
 the ``plan_json`` system-block is always attached for PLAN,
 DECOMPOSE, and REPLAN — including when a project ships a
 custom plan template that drops the schema;
 project overrides cannot silently remove the machine contract.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add pagination to the user listing endpoint."

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def _split(self, rendered: str) -> tuple[str, str, str]:
        return rendered.partition("<orcho:system-block")

    # ── PLAN ──────────────────────────────────────────────────────────────

    def test_plan_schema_only_in_system_tail(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_prompt(task, "/project", plugin).text
        user_part, marker, system_part = self._split(out)
        assert marker, "plan_json system-tail boundary not found"
        # User-editable parts MUST NOT carry parser-critical schema.
        assert "exactly one JSON object" not in user_part
        assert "short_summary" not in user_part
        assert "planning_context" not in user_part
        assert "depends_on" not in user_part
        assert "fenced code block" not in user_part
        # System-tail MUST carry them.
        assert 'name="plan_json"' in system_part
        assert "exactly one JSON object" in system_part
        assert "short_summary" in system_part
        assert "planning_context" in system_part

    def test_decompose_schema_only_in_system_tail(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.decompose_plan_prompt(task, "/project", plugin).text
        user_part, marker, system_part = self._split(out)
        assert marker
        assert "exactly one JSON object" not in user_part
        assert "short_summary" not in user_part
        assert "depends_on" not in user_part
        assert 'name="plan_json"' in system_part
        assert "exactly one JSON object" in system_part
        assert "depends_on" in system_part

    def test_replan_schema_attached_even_when_template_silent(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.replan_prompt(
            task, "Missing rollback strategy.", "", "/project", plugin,
        ).text
        user_part, marker, system_part = self._split(out)
        assert marker, (
            "REPLAN must always attach plan_json contract — the parser "
            "validates every round, not just round 1"
        )
        assert "exactly one JSON object" not in user_part
        assert 'name="plan_json"' in system_part
        assert "exactly one JSON object" in system_part

    def test_replan_attaches_plan_artifact_boundary(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """M11.5 Fix 4: REPLAN emits a plan artifact just like PLAN
        and must therefore carry the same protected boundary contract.
        Without this contract a replan round can silently rewrite the
        plan file directly, bypassing the orchestration policy.
        """
        out = prompts.replan_prompt(
            task, "Missing rollback strategy.", "", "/project", plugin,
        ).text
        assert 'name="plan_artifact_boundary"' in out
        assert "Do not call Write or Edit" in out

    def test_plan_and_replan_share_plan_artifact_boundary(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """The plan_artifact_boundary block is identical between PLAN
        and REPLAN — both surfaces must attach the same code-owned
        boundary text byte-for-byte.
        """
        plan_out = prompts.plan_prompt(task, "/project", plugin).text
        replan_out = prompts.replan_prompt(
            task, "Missing rollback strategy.", "", "/project", plugin,
        ).text
        marker_open = '<orcho:system-block kind="contract" name="plan_artifact_boundary"'
        # Both prompts include the block.
        assert marker_open in plan_out
        assert marker_open in replan_out

    # ── Custom plan template path ─────────────────────────────────────────

    def test_custom_plan_template_still_gets_contract(
        self, task: str, tmp_path: Path,
    ) -> None:
        """A plugin-provided custom plan template (``custom_plan_prompt_file``)
 bypasses the shipped ``architect_plan.md``. The machine contract
 must still be attached via system-tail so a custom template
 cannot silently drop the parser shape.
 """
        custom = tmp_path / "my_plan.md"
        custom.write_text(
            "CUSTOM PROJECT PLAN TEMPLATE — task: {task}\n"
            "(no schema directives in this template)\n"
        )
        plugin = PluginConfig(custom_plan_prompt_file="my_plan.md")
        out = prompts.plan_prompt(task, str(tmp_path), plugin).text
        # ADR 0028 / M10.5 Step 5: protected contracts now lead the
        # wire. The custom template body is wrapped as a
        # ``minimal_intent`` part (TURN/NONE) and lands after the
        # contracts. Assert presence + contract block + ordering
        # invariant (contracts precede the custom template).
        assert "CUSTOM PROJECT PLAN TEMPLATE" in out
        assert 'name="plan_json"' in out
        assert "exactly one JSON object" in out
        assert out.find('name="plan_json"') < out.find(
            "CUSTOM PROJECT PLAN TEMPLATE",
        )

    # ── Parser stays parseable ────────────────────────────────────────────

    def test_plan_parser_still_accepts_canonical_json(self) -> None:
        """Sanity check: the parser contract that ``plan_json_contract``
 embeds is the same shape ``parse_plan`` validates.
 """
        from pipeline.plan_parser import parse_plan

        raw = (
            '{"short_summary": "s", "planning_context": "ctx", '
            '"tasks": [{"id": "t1", "goal": "g"}]}'
        )
        parsed = parse_plan(raw)
        assert parsed.short_summary == "s"
        assert len(parsed.subtasks) == 1


class TestArchitectFamilyMigration:
    """ADR 0009 PLAN / REPLAN render through composed parts.

 Pins:
 PLAN default renders from
 ``roles/systems_architect`` + ``tasks/plan`` + ``formats/detailed``
 and is equivalent to passing the same spec explicitly;
 REPLAN default renders from
 ``roles/systems_architect`` + ``tasks/replan`` + ``formats/detailed``;
 JSON output shape stays code-owned via ``plan_json_contract``
 project overrides of the user-editable parts cannot drop
 the parser contract;
 mock provider detectors (``_is_plan_prompt`` /
 ``_is_replan_prompt``) still match the new templates;
 legacy flat overrides ``architect_plan.md`` /
 ``architect_replan.md`` still win when present.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add pagination to the user listing endpoint."

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    @pytest.fixture
    def critique(self) -> str:
        return "Plan missing rollback path for the new endpoint."

    # ── PLAN ──────────────────────────────────────────────────────────────

    def test_plan_default_matches_explicit_spec(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        baseline = prompts.plan_prompt(task, "/project", plugin).text
        explicit = prompts.plan_prompt(
            task, "/project", plugin,
            prompt_spec=PromptSpec(
                role="systems_architect", task="plan", format="detailed",
            ),
        ).text
        assert baseline == explicit

    def test_plan_uses_composed_architect_parts(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_prompt(task, "/project", plugin).text
        assert "You are the solution architect for this task." in out
        # ADR 0028 / M10.5 Step 2: TASK TO PLAN header is emitted by
        # the builder as a typed turn_input part body; static plan.md
        # method prose now reads "implementation plan for the task
        # before any code lands".
        assert "TASK TO PLAN:" in out
        assert task in out
        assert "implementation plan for the task before any code lands" in out

    def test_plan_no_placeholder_leaks(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_prompt(task, "/project", plugin).text
        for var in (
            "$task", "$context", "$ma_artifacts_dir", "$extra_step",
            "$plan_schema_doc", "$artifact_language", "$task_language",
            "$project_dir",
        ):
            assert var not in out, f"literal {var} leaked into PLAN render"


    def test_replan_default_matches_explicit_spec(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        baseline = prompts.replan_prompt(task, critique, "", "/project", plugin).text
        explicit = prompts.replan_prompt(
            task, critique, "", "/project", plugin,
            prompt_spec=PromptSpec(
                role="systems_architect", task="replan", format="detailed",
            ),
        ).text
        assert baseline == explicit

    def test_replan_uses_composed_architect_parts(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        out = prompts.replan_prompt(task, critique, "", "/project", plugin).text
        assert "You are the solution architect for this task." in out
        # tasks/replan.md teaches reconciliation between reviewer critique
        # and human feedback; the static body opens with the retry frame.
        assert "You are revising the plan for another attempt." in out
        assert critique in out
        assert (
            "Apply human feedback as authoritative operator guidance."
            in out
        )

    def test_replan_no_placeholder_leaks(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        out = prompts.replan_prompt(task, critique, "", "/project", plugin).text
        for var in (
            "$task", "$context", "$critique", "$task_language",
            "$artifact_language", "$project_dir",
        ):
            assert var not in out, f"literal {var} leaked into REPLAN render"


    def test_mock_provider_detects_plan_after_composition(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """The mock agent provider's phase detector
 (``_is_plan_prompt``) keys on the rendered prompt's PLAN header,
 opening directive, and code-owned plan artifact boundary block.
 """
        from agents.runtimes._strategy import (
            _is_build_prompt,
            _is_plan_prompt,
            _is_replan_prompt,
        )
        out = prompts.plan_prompt(task, "/project", plugin).text
        assert _is_plan_prompt(out)
        assert not _is_replan_prompt(out)
        assert not _is_build_prompt(out)

    def test_mock_provider_detects_replan_after_composition(
        self, task: str, plugin: PluginConfig, critique: str,
    ) -> None:
        from agents.runtimes._strategy import (
            _is_build_prompt,
            _is_plan_prompt,
            _is_replan_prompt,
        )
        out = prompts.replan_prompt(task, critique, "", "/project", plugin).text
        assert _is_replan_prompt(out)
        assert not _is_plan_prompt(out)
        assert not _is_build_prompt(out)


class TestDecomposeFamilyMigration:
    """ADR 0009 DECOMPOSE renders through composed parts.

 Pins:
 default renders from
 ``roles/systems_architect`` + ``tasks/decompose`` + ``formats/detailed``
 and is equivalent to passing the same spec explicitly;
 skill roster handling is preserved on both branches:
 registered skills appear in the AVAILABLE SKILLS section;
 empty roster gets the "none registered" guidance;
 JSON output shape (including DAG invariants — unique ids,
 depends_on validity, acyclicity) stays code-owned via
 ``plan_json_contract`` system-tail; user-editable parts
 cannot drop the parser contract;
 legacy flat override ``architect_decompose.md`` still wins
 when present, and the system-tail contract still attaches.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add pagination + caching to the /users endpoint."

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    @pytest.fixture
    def plugin_with_skill(self) -> PluginConfig:
        from pipeline.skills import SkillPackage
        plg = PluginConfig()
        plg.skill_registry = {
            "backend": SkillPackage(
                name="backend",
                description="Adds REST endpoints.",
                root_dir=Path("/tmp/skills/backend"),
                skill_md_path=Path("/tmp/skills/backend/SKILL.md"),
                body="",
                frontmatter={
                    "name": "backend",
                    "description": "Adds REST endpoints.",
                },
                source="project",
                checksum="sha256:backend",
            ),
        }
        return plg

    def test_default_matches_explicit_spec(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        baseline = prompts.decompose_plan_prompt(task, "/project", plugin).text
        explicit = prompts.decompose_plan_prompt(
            task, "/project", plugin,
            prompt_spec=PromptSpec(
                role="systems_architect", task="decompose", format="detailed",
            ),
        ).text
        assert baseline == explicit

    def test_uses_composed_architect_parts(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.decompose_plan_prompt(task, "/project", plugin).text
        assert "You are the solution architect for this task." in out
        assert "TASK TO DECOMPOSE:" in out
        assert task in out
        assert "directed acyclic graph (DAG) of subtasks" in out

    def test_no_placeholder_leaks(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.decompose_plan_prompt(task, "/project", plugin).text
        for var in (
            "$task", "$context", "$skill_roster_block", "$extra_step",
            "$plan_schema_doc", "$artifact_language", "$task_language",
            "$project_dir",
        ):
            assert var not in out, (
                f"literal {var} leaked into DECOMPOSE render"
            )

    def test_skill_roster_present_when_skills_registered(
        self, task: str, plugin_with_skill: PluginConfig,
    ) -> None:
        out = prompts.decompose_plan_prompt(
            task, "/project", plugin_with_skill,
        ).text
        assert "AVAILABLE SKILLS" in out
        assert "`backend`" in out
        assert "Adds REST endpoints" in out

    def test_skill_routing_contract_attaches(
        self, task: str, plugin_with_skill: PluginConfig,
    ) -> None:
        out = prompts.decompose_plan_prompt(
            task, "/project", plugin_with_skill,
        ).text
        assert 'name="skill_routing"' in out
        assert "AVAILABLE SKILLS list" in out
        assert "full skill bodies are injected later" in out
        assert "subtask `skill` field" in out

    def test_empty_roster_guidance_when_no_skills(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        out = prompts.decompose_plan_prompt(task, "/project", plugin).text
        assert "AVAILABLE SKILLS" in out
        assert "none registered" in out
        assert "omit `skill` on subtasks" in out

    def test_schema_only_in_system_tail(
        self, task: str, plugin: PluginConfig,
    ) -> None:
        """The plan_json contract carries the schema body, not the
 user-editable parts.
 """
        out = prompts.decompose_plan_prompt(task, "/project", plugin).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker, "plan_json system-tail boundary not found"
        # User template MUST NOT carry the JSON schema body.
        assert "exactly one JSON object" not in user_part
        assert "short_summary" not in user_part
        assert "depends_on" not in user_part
        assert "fenced code block" not in user_part
        # System-tail carries the canonical contract.
        assert 'name="plan_json"' in system_part
        assert "exactly one JSON object" in system_part
        assert "short_summary" in system_part
        assert "depends_on" in system_part



class TestArchitectUtilityFamilyMigration:
    """ADR 0009 A — architect utility prompts (HYPOTHESIS,
 READONLY PLAN) render through composed parts.

 These are code-default builders. HYPOTHESIS can also receive the
 owning plan step's ``prompt_spec`` so the profile controls its
 detail style; otherwise the builder falls back to its default spec.

 Pins:
 HYPOTHESIS composes ``roles/systems_architect`` +
 ``tasks/hypothesis`` + ``formats/terse``;
 READONLY PLAN composes ``roles/systems_architect`` +
 ``tasks/readonly_plan`` + ``formats/detailed``;
 no ``$var`` placeholder leaks;
 architect persona reused from;
 codemap section and language directive preserved;
 legacy flat overrides win; system-tail blocks still attach
 on the legacy path.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Fix calc.add returning wrong result for negative inputs."

    @pytest.fixture
    def codemap(self) -> str:
        return "calc.py\n  add(a, b)"

    # ── HYPOTHESIS ────────────────────────────────────────────────────────

    def test_hypothesis_uses_composed_architect_parts(
        self, task: str, codemap: str,
    ) -> None:
        out = prompts.hypothesis_prompt(task, "/project", codemap=codemap).text
        assert "You are the solution architect for this task." in out
        assert "Produce a SHORT hypothesis" in out
        assert "no preamble, no headings" in out
        assert task in out
        assert "calc.py" in out

    def test_hypothesis_uses_terse_format_preset(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_prompt(task, "/project").text
        # formats/terse content anchor.
        assert "Keep the response concise" in out

    def test_hypothesis_can_reuse_plan_prompt_format(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_prompt(
            task,
            "/project",
            prompt_spec=PromptSpec(
                role="systems_architect",
                task="plan",
                format="detailed",
            ),
        ).text
        assert "Produce a SHORT hypothesis" in out
        assert "Separate findings" in out

    def test_hypothesis_can_use_profile_owned_compact_format(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_prompt(
            task,
            "/project",
            prompt_spec=PromptSpec(
                role="systems_architect",
                task="plan",
                format="terse",
            ),
            format_name="compact",
        ).text
        assert "Produce a SHORT hypothesis" in out
        assert "decision-oriented" in out
        assert "Keep the response concise" not in out

    def test_hypothesis_no_placeholder_leaks(
        self, task: str, codemap: str,
    ) -> None:
        out = prompts.hypothesis_prompt(task, "/project", codemap=codemap).text
        for var in (
            "$task", "$context", "$codemap_section", "$task_language",
            "$project_dir",
        ):
            assert var not in out, (
                f"literal {var} leaked into HYPOTHESIS render"
            )

    def test_hypothesis_no_codemap_section_when_codemap_empty(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_prompt(task, "/project").text
        # When codemap is empty, the REPO MAP section is fully absent.
        assert "REPO MAP" not in out


    def test_readonly_plan_uses_composed_architect_parts(
        self, task: str, codemap: str,
    ) -> None:
        out = prompts.readonly_plan_prompt(
            task, "/project", codemap=codemap,
        ).text
        assert "You are the solution architect for this task." in out
        assert "concrete implementation plan in Markdown" in out
        assert "read-only planning pass" in out
        assert task in out
        assert "calc.py" in out

    def test_readonly_plan_uses_detailed_format_preset(
        self, task: str,
    ) -> None:
        out = prompts.readonly_plan_prompt(task, "/project").text
        # formats/detailed content anchor.
        assert "detailed response" in out.lower() or "Separate findings" in out

    def test_readonly_plan_no_placeholder_leaks(
        self, task: str, codemap: str,
    ) -> None:
        out = prompts.readonly_plan_prompt(
            task, "/project", codemap=codemap,
        ).text
        for var in (
            "$task", "$context", "$codemap_section", "$task_language",
            "$project_dir",
        ):
            assert var not in out, (
                f"literal {var} leaked into READONLY PLAN render"
            )

    def test_readonly_plan_carries_change_handoff_system_tail(
        self, task: str,
    ) -> None:
        """READONLY PLAN is a runtime planning surface — agents shouldn't
 commit/push during a read-only pass. System-tail
 change_handoff still applies.
 """
        out = prompts.readonly_plan_prompt(task, "/project").text
        assert '<orcho:system-block kind="strategy" name="change_handoff"' in out



class TestReviewerUtilityMigration:
    """ADR 0009 B — reviewer utility prompts compose through parts.

    Migrated:
    ``hypothesis_review_focus`` — HYPOTHESIS QA reviewer.
    ``runtime_review_uncommitted_prompt`` — runtime
    configured-change-target review surface.
    ``hypothesis_file_review_prompt`` / ``plan_file_review_prompt`` —
    phase-specific artifact review surfaces.

    These default to ``formats/detailed`` as diagnostic reviewer
    surfaces, but callers can select a narrower format when a profile
    owns the surrounding planning surface.

    System-tail contracts (``review_json`` always; ``review_target``
    on the uncommitted surface only) stay code-owned. The
    user-editable tasks carry NO parser contract, NO language
    directive, NO system-tail block names.
    """

    @pytest.fixture
    def task(self) -> str:
        return "Fix calc.add returning wrong result for negative inputs."

    # ── HYPOTHESIS QA ─────────────────────────────────────────────────────

    def test_hypothesis_qa_uses_composed_reviewer_parts(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_review_focus(task, project_dir="/project").text
        assert "You are the application architect for this task." in out
        assert "Validate this implementation hypothesis" in out
        assert task in out

    def test_hypothesis_qa_uses_detailed_format_preset(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_review_focus(task, project_dir="/project").text
        # formats/detailed content anchor.
        assert "detailed response" in out.lower() or "Separate findings" in out

    def test_hypothesis_qa_can_use_terse_format_preset(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_review_focus(
            task,
            project_dir="/project",
            format_name="terse",
        ).text
        assert "Keep the response concise" in out
        assert "Separate findings" not in out

    def test_hypothesis_qa_can_use_compact_format_preset(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_review_focus(
            task,
            project_dir="/project",
            format_name="compact",
        ).text
        assert "decision-oriented" in out
        assert "Separate findings" not in out

    def test_hypothesis_qa_no_placeholder_leaks(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_review_focus(task, project_dir="/project").text
        for var in ("$task", "$context", "$task_language", "$project_dir"):
            assert var not in out, (
                f"literal {var} leaked into HYPOTHESIS QA render"
            )

    def test_hypothesis_qa_review_json_contract_in_system_tail(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_review_focus(task, project_dir="/project").text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker
        # JSON contract markers must NOT appear in the user-editable
        # portion — they live in review_json_contract.
        assert "exactly one JSON object" not in user_part
        assert '"verdict":' not in user_part
        # ...and MUST appear in system-tail.
        assert 'name="review_json"' in system_part
        assert "exactly one JSON object" in system_part
        assert "APPROVED" in system_part
        assert "REJECTED" in system_part

    def test_hypothesis_file_review_uses_normal_validation_surface_plus_artifact(
        self, task: str,
    ) -> None:
        # M4: the file wrapper composes the normal validate_hypothesis
        # task (no more validate_hypothesis_file markdown) and attaches
        # the reviewed file as a separate artifact PromptPart appended
        # to the wire prompt and to the prompt_trace render envelope.
        # Path is metadata-only on the artifact part (see
        # ``test_dynamic_artifact_parts.TestArtifactWirePresence``);
        # only the artifact body lands on the wire.
        out = prompts.hypothesis_file_review_prompt(
            "/tmp/hypothesis.md",
            "# Hypothesis\n\nLikely payload mismatch.",
            task,
            project_dir="/project",
        ).text
        assert "You are the application architect for this task." in out
        # Normal validate_hypothesis task anchor (no "below" suffix —
        # that lived in the deleted validate_hypothesis_file template).
        assert "Validate this implementation hypothesis" in out
        # Artifact body appears once on the wire.
        assert "Likely payload mismatch" in out
        assert out.count("Likely payload mismatch") == 1
        # Path is NOT in the wire — it travels as the artifact part's
        # metadata-only ``artifact_path`` field.
        assert "/tmp/hypothesis.md" not in out
        assert 'name="review_json"' in out

    def test_hypothesis_file_review_can_use_compact_format(
        self, task: str,
    ) -> None:
        out = prompts.hypothesis_file_review_prompt(
            "/tmp/hypothesis.md",
            "# Hypothesis\n\nLikely payload mismatch.",
            task,
            project_dir="/project",
            format_name="compact",
        ).text
        assert "decision-oriented" in out
        assert "Separate findings" not in out


    def test_review_uncommitted_uses_composed_reviewer_parts(self) -> None:
        out = prompts.runtime_review_uncommitted_prompt(
            focus="check auth race",
            project_dir="/project",
        ).text
        assert "You are the application architect for this task." in out
        assert "Review the configured code-change target" in out
        assert "read-only review pass" in out
        assert "check auth race" in out

    def test_review_uncommitted_no_placeholder_leaks(self) -> None:
        out = prompts.runtime_review_uncommitted_prompt(
            focus="check auth race",
            project_dir="/project",
        ).text
        for var in (
            "$focus", "$context", "$task_language", "$project_dir",
        ):
            assert var not in out, (
                f"literal {var} leaked into REVIEW UNCOMMITTED render"
            )

    def test_review_uncommitted_review_target_only_in_system_tail(self) -> None:
        """``review_target_strategy`` tells the reviewer which surface
 (working tree / commit / commit_set) to inspect. That policy
 is code-owned — the user-editable task must NOT reference the
 block by name.
 """
        out = prompts.runtime_review_uncommitted_prompt(
            focus="any",
            project_dir="/project",
        ).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker
        # The user-editable task should NOT name the system-tail block.
        # (Reading "review_target" anywhere in user_part would indicate
        # the task is talking about orchestration policy.)
        assert "review_target" not in user_part
        # System-tail carries both review_target (mode policy) and
        # review_json (parser contract).
        assert 'name="review_target"' in system_part
        assert 'name="review_json"' in system_part

    def test_plan_file_review_uses_normal_validation_surface_with_typed_views(
        self, task: str,
    ) -> None:
        # PR3 cutover: the wrapper composes the normal validate_plan
        # task and emits two typed views rendered from ParsedPlan
        # (plan_contract:typed_plan + plan_tasks:execution_plan)
        # instead of a monolithic plan-markdown artifact part. The
        # on-disk ``plan_*.md`` is presentation-only evidence now;
        # the reviewer reads typed views, not parsed prose.
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        plan = ParsedPlan(
            short_summary="Stub plan.",
            planning_context="Context.",
            subtasks=(
                SubTask(id="t1", goal="Change payload key."),
            ),
            source="json",
            goal="Fix the payload key drift",
            acceptance_criteria=("Payload normalizes to camelCase",),
        )
        out = prompts.plan_file_review_prompt(
            plan,
            task,
            PluginConfig(),
            project_dir="/project",
        ).text
        assert "You are the solution architecture reviewer for this task." in out
        assert "implementation diff yet" in out
        assert "sub-agents" in out
        # Normal validate_plan task anchor (ADR 0028 / M10.5 Step 2
        # rewording: "Review the implementation plan against the task.").
        assert "Review the implementation plan against the task" in out
        # Subtask body still travels on the wire — once, in the
        # plan_tasks view.
        assert "Change payload key" in out
        assert out.count("Change payload key") == 1
        # No on-disk path token leaks into the wire — the new
        # signature does not even accept one.
        assert "/tmp/plan.md" not in out
        assert "File: " not in out
        assert 'name="review_json"' in out



class TestCrossPlanFamilyMigration:
    """ADR 0009 B — CROSS PLAN renders through composed parts.

 A lifted the ``=== SUBTASK [<alias>] ===... === END ===``
 machine grammar into the ``cross_subtask_blocks`` system-tail
 contract. B is the safe composable-parts migration on top
 of that foundation.

 Pins:
 composes ``roles/systems_architect`` + ``tasks/cross_plan`` +
 ``formats/detailed``;
 default render is equivalent to passing the same spec
 explicitly;
 ``cross_subtask_blocks`` system-tail contract still attaches;
 parser (``extract_subtasks``) still works against an agent
 response that follows the contract grammar;
 legacy flat override ``cross_architect_plan.md`` still wins,
 and system-tail contract still attaches on legacy path;
 no ``$var`` placeholder leaks.
 """

    @pytest.fixture
    def task(self) -> str:
        return "Add pagination to /users across api and client."

    @pytest.fixture
    def projects(self):
        return {"api": Path("/proj/api"), "client": Path("/proj/client")}

    @pytest.fixture
    def cross_artifacts_dir(self) -> Path:
        return Path("/proj/.orcho/cross")

    def test_uses_composed_architect_parts(
        self, task: str, projects, cross_artifacts_dir: Path,
    ) -> None:
        from pipeline.cross_project.orchestrator import cross_plan_prompt
        out = cross_plan_prompt(task, projects, cross_artifacts_dir).text
        # Architect persona reused from roles/systems_architect.md.
        assert "You are the solution architect for this task." in out
        # Cross-project framing — pin on semantic signal, not specific
        # wording, so future prompt polishing doesn't break this test
        # on cosmetic edits.
        assert "spans multiple codebases" in out or "CROSS-PROJECT" in out
        assert "PROJECTS INVOLVED" in out
        # Task / project list interpolated.
        assert task in out
        assert "/proj/api" in out
        assert "/proj/client" in out

    def test_no_placeholder_leaks(
        self, task: str, projects, cross_artifacts_dir: Path,
    ) -> None:
        from pipeline.cross_project.orchestrator import cross_plan_prompt
        out = cross_plan_prompt(task, projects, cross_artifacts_dir).text
        for var in (
            "$task", "$paths_list", "$context", "$cross_artifacts_dir",
            "$aliases", "$alias1", "$alias2", "$project_dir",
            "$task_language",
        ):
            assert var not in out, (
                f"literal {var} leaked into CROSS PLAN render"
            )

    def test_cross_plan_json_contract_in_system_tail(
        self, task: str, projects, cross_artifacts_dir: Path,
    ) -> None:
        """ADR 0054: the cross architect emits a typed JSON object. The
        machine-output shape lives in the code-owned system tail, not in
        the user-editable parts; the legacy SUBTASK marker grammar is
        gone everywhere.
        """
        from pipeline.cross_project.orchestrator import cross_plan_prompt
        out = cross_plan_prompt(task, projects, cross_artifacts_dir).text
        user_part, marker, system_part = out.partition("<orcho:system-block")
        assert marker
        # The deleted marker grammar must not survive anywhere.
        assert "=== SUBTASK [" not in out
        assert "=== END ===" not in out
        # System-tail MUST carry the JSON contract + schema keys.
        assert 'name="cross_plan_json"' in system_part
        assert "interface_contract" in system_part
        assert '"subtasks"' in system_part

    def test_parser_parses_typed_cross_plan_json(
        self, task: str, projects, cross_artifacts_dir: Path,
    ) -> None:
        """``parse_cross_plan`` validates a typed JSON object against the
        ADR 0054 schema and exposes a per-alias subtask map. A response
        that follows the JSON contract must parse cleanly.
        """
        import json

        from pipeline.cross_project.orchestrator import cross_plan_prompt
        from pipeline.cross_project.plan_parser import parse_cross_plan
        # Render once just to confirm prompt is well-formed.
        _ = cross_plan_prompt(task, projects, cross_artifacts_dir)

        aliases = list(projects.keys())
        agent_response = json.dumps({
            "short_summary": "Add paginated users across api + client.",
            "interface_contract": "GET /users?page=X returns {items, next}.",
            "implementation_order": ["Change api", "Change client"],
            "subtasks": [
                {
                    "alias": "api",
                    "goal": "Add /users?page=X endpoint.",
                    "spec": "Add the paginated endpoint in src/api/users.py.",
                    "depends_on": [],
                    "files": ["[api]/src/api/users.py"],
                    "produces": "page payload",
                    "consumes": "",
                },
                {
                    "alias": "client",
                    "goal": "Add pagination state.",
                    "spec": "Wire pagination state in src/client/users.tsx.",
                    "depends_on": ["api"],
                    "files": ["[client]/src/client/users.tsx"],
                    "produces": "",
                    "consumes": "page payload",
                },
            ],
        })
        result = parse_cross_plan(agent_response, aliases)
        subtasks = result.parsed.subtasks_dict()
        assert subtasks["api"] is not None
        assert "src/api/users.py" in subtasks["api"]
        assert subtasks["client"] is not None
        assert "src/client/users.tsx" in subtasks["client"]
        deps = dict(result.parsed.dependencies)
        assert deps["client"] == ("api",)



class TestLegacyFlatOverrideRemoved:
    """ADR 0009 legacy root-level flat prompt names
 (``developer_build``, ``reviewer_code_review``, etc.) have been
 removed. Builders no longer accept a ``legacy_name`` fallback,
 and the corresponding shipped files have been deleted.

 A project that still ships a flat-style override at
 ``.orcho/multiagent/prompts/<legacy_name>.md`` has zero effect on
 the rendered prompt. Project authors must migrate their overrides
 to the composable-part layout (``roles/`` / ``tasks/`` /
 ``formats/``).
 """

    def test_flat_developer_build_override_does_not_affect_build_prompt(
        self, tmp_path: Path,
    ) -> None:
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts"
        override_dir.mkdir(parents=True)
        # Drop a stale flat-name override into the prompts root.
        (override_dir / "developer_build.md").write_text(
            "STALE-FLAT-OVERRIDE — must not affect render"
        )
        out = prompts.build_prompt(
            "Add pagination", str(tmp_path), PluginConfig(),
        ).text
        # Composed parts render unaffected.
        # Phase Q1a + Q2 P0: role file is ``implementation_engineer.md``.
        assert "You are the implementation engineer for this task." in out
        # ADR 0028 / M10.5 Step 2: TASK header lives in the typed
        # turn_input part body; the task string appears in the wire.
        assert "TASK:" in out
        assert "Add pagination" in out
        # Stale override silently ignored.
        assert "STALE-FLAT-OVERRIDE" not in out

    def test_flat_reviewer_code_review_override_does_not_affect_review_focus(
        self, tmp_path: Path,
    ) -> None:
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts"
        override_dir.mkdir(parents=True)
        (override_dir / "reviewer_code_review.md").write_text(
            "STALE-FLAT-OVERRIDE — must not affect render"
        )
        out = prompts.review_focus(
            "Add pagination", PluginConfig(), project_dir=str(tmp_path),
        ).text
        assert "You are the application architect for this task." in out
        # System-tail JSON contract still attaches.
        assert 'name="review_json"' in out
        # Stale override silently ignored.
        assert "STALE-FLAT-OVERRIDE" not in out


class TestVerificationContractPromptInjection:
    """T4: phase-limited verification block reaches phase prompts.

    Drives the real adapter call path (``adapters.run_*`` → builder) with a
    capturing fake agent so the assertion is against the actual wire bytes the
    runtime would receive — not a direct builder call.
    """

    @staticmethod
    def _contract_state(extra_phase: str):
        from types import SimpleNamespace

        from pipeline.verification_contract import (
            PlaceholderContext,
            VerificationContract,
        )

        plugin = PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification={
                "commands": {"lint": {"run": "ruff check {checkout}", "env": "ci"}},
                "schedule": [
                    {"before_phase": extra_phase,
                     "policy": "warn", "commands": ["lint"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        return SimpleNamespace(extras={
            "verification_contract": contract,
            "verification_placeholders": PlaceholderContext(checkout="/checkout"),
        })

    class _CapturingAgent:
        def __init__(self) -> None:
            self.captured = ""
            self.session_id = None

        def invoke(self, text: str, cwd: str, **kwargs) -> str:
            self.captured = text
            return "ok"

    def test_helper_returns_run_scoped_part_from_extras(self) -> None:
        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )
        from pipeline.prompts.types import PromptCacheScope, PromptStability

        state = self._contract_state("implement")
        part = _verification_contract_part(state, "implement")

        assert part is not None
        assert part.stability is PromptStability.RUN
        assert part.cache_scope is PromptCacheScope.SESSION
        # Placeholder resolved syntactically from the PlaceholderContext.
        assert "ruff check /checkout" in part.body

    def test_helper_returns_none_without_contract(self) -> None:
        from types import SimpleNamespace

        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )

        state = SimpleNamespace(extras={})
        assert _verification_contract_part(state, "implement") is None

    def test_implement_prompt_carries_resolved_block_via_adapter(self) -> None:
        from pipeline.phases import adapters
        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )

        state = self._contract_state("implement")
        part = _verification_contract_part(state, "implement")
        agent = self._CapturingAgent()

        adapters.run_build(
            agent, "Add pagination", "/proj", PluginConfig(),
            verification_part=part,
        )

        assert "Verification contract — implement:" in agent.captured
        assert "ruff check /checkout" in agent.captured

    def test_plan_prompt_carries_resolved_block_via_adapter(self) -> None:
        from pipeline.phases import adapters
        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )

        state = self._contract_state("plan")
        part = _verification_contract_part(state, "plan")
        agent = self._CapturingAgent()

        adapters.run_plan(
            agent, "Add pagination", "/proj", PluginConfig(),
            verification_part=part,
        )

        assert "Verification contract — plan:" in agent.captured
        assert "ruff check /checkout" in agent.captured

    def test_no_contract_leaves_prompt_bytes_unchanged(self) -> None:
        from pipeline.phases import adapters

        baseline_agent = self._CapturingAgent()
        adapters.run_build(baseline_agent, "Add pagination", "/proj", PluginConfig())

        none_agent = self._CapturingAgent()
        adapters.run_build(
            none_agent, "Add pagination", "/proj", PluginConfig(),
            verification_part=None,
        )

        assert none_agent.captured == baseline_agent.captured
        assert "Verification contract" not in none_agent.captured

    def test_phase_block_is_limited_to_its_phase(self) -> None:
        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )

        # Contract schedules a before_phase entry only for ``plan``.
        state = self._contract_state("plan")
        # ``implement`` is not the scheduled phase → no block for it.
        assert _verification_contract_part(state, "implement") is None
        assert _verification_contract_part(state, "plan") is not None

    @staticmethod
    def _delivery_state():
        from types import SimpleNamespace

        from pipeline.verification_contract import (
            PlaceholderContext,
            VerificationContract,
        )

        plugin = PluginConfig(
            verification_envs={"ci": {}},
            verification={
                "commands": {
                    "smoke": {"run": "pytest -q {checkout}/smoke", "env": "ci"},
                },
                "schedule": [
                    {"before_delivery": True, "policy": "warn",
                     "commands": ["smoke"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        return SimpleNamespace(extras={
            "verification_contract": contract,
            "verification_placeholders": PlaceholderContext(checkout="/checkout"),
        })

    def test_before_delivery_block_reaches_final_acceptance_via_adapter(
        self,
    ) -> None:
        # final_acceptance runs through adapters.run_review (output_contract=
        # "release"); the before_delivery block must reach that real wire.
        from pipeline.phases import adapters
        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )

        state = self._delivery_state()
        part = _verification_contract_part(state, "final_acceptance")
        assert part is not None
        agent = self._CapturingAgent()

        adapters.run_review(
            agent, "[final_acceptance] Ship it", "/proj", PluginConfig(),
            output_contract="release",
            verification_part=part,
        )

        assert "Verification contract — final_acceptance:" in agent.captured
        assert "pytest -q /checkout/smoke" in agent.captured

    def test_before_delivery_not_shown_for_non_final_phase(self) -> None:
        from pipeline.phases.builtin.prompt_parts import (
            _verification_contract_part,
        )

        state = self._delivery_state()
        # before_delivery is delivery-scoped — implement/plan see nothing.
        assert _verification_contract_part(state, "implement") is None
        assert _verification_contract_part(state, "final_acceptance") is not None


class TestScopeExpansionPromptEvidence:
    """F2: the scope-expansion block reaches the final_acceptance reviewer.

    The handler appends the rendered scope-expansion assessment to the
    ``readiness_summary`` it passes to ``adapters.run_review`` (output_contract
    ``release``). This asserts that surface carries the evidence onto the wire,
    and that an empty block leaves the readiness prompt untouched.
    """

    class _CapturingAgent:
        def __init__(self) -> None:
            self.captured = ""
            self.session_id = None

        def invoke(self, text: str, cwd: str, **kwargs) -> str:
            self.captured = text
            return "ok"

    def test_scope_expansion_lines_reach_final_acceptance_wire(self) -> None:
        from pipeline.engine.scope_expansion import (
            FileScopeSignals,
            build_scope_expansion_assessment,
        )
        from pipeline.phases import adapters
        from pipeline.phases.builtin.scope_expansion_support import (
            render_scope_expansion_text,
        )

        assessment = build_scope_expansion_assessment([
            FileScopeSignals(
                path="package-lock.json",
                category="build",
                verified=True,
                has_explanation=True,
            ),
            FileScopeSignals(
                path="storage/cache.py",
                category="persistence",
                is_persistence=True,
            ),
        ])
        scope_text = render_scope_expansion_text(assessment)
        agent = self._CapturingAgent()

        adapters.run_review(
            agent, "[final_acceptance] Ship it", "/proj", PluginConfig(),
            output_contract="release",
            readiness_summary=scope_text,
        )

        assert "Scope expanded:" in agent.captured
        assert "package-lock.json — build" in agent.captured
        assert "Scope expansion blocker:" in agent.captured
        assert "storage/cache.py — persistence" in agent.captured

    def test_empty_scope_block_adds_nothing_to_wire(self) -> None:
        from pipeline.engine.scope_expansion import ScopeExpansionAssessment
        from pipeline.phases import adapters
        from pipeline.phases.builtin.scope_expansion_support import (
            render_scope_expansion_text,
        )

        scope_text = render_scope_expansion_text(ScopeExpansionAssessment())
        assert scope_text == ""
        agent = self._CapturingAgent()

        adapters.run_review(
            agent, "[final_acceptance] Ship it", "/proj", PluginConfig(),
            output_contract="release",
            readiness_summary=scope_text,
        )

        assert "Scope expand" not in agent.captured


# ── T3: allowed_modifications block reaches every review surface ──────────

_AM_HEADER = "## Allowed Companion Modifications"
_AM_ENTRY = "package-lock.json — derived from package.json"


def _am_plugin() -> PluginConfig:
    return PluginConfig(allowed_modifications=[_AM_ENTRY])


class TestAllowedModificationsBlockWiring:
    """T3: the project-declared allowed-companion-modifications block is
    surfaced in every review prompt — review_focus (review_changes +
    final_acceptance), plan_file_review_prompt (the typed validate_plan
    path), plan_review_focus (the diff-only fallback), and the assembled
    uncommitted review_changes wire — when the list is non-empty, and is
    byte-invisible when the list is empty."""

    def _parsed_plan_with_contract(self):
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        return ParsedPlan(
            short_summary="Stub plan.",
            planning_context="Context.",
            subtasks=(SubTask(id="t1", goal="Change payload key."),),
            source="json",
            goal="Fix the payload key drift",
            acceptance_criteria=("Payload normalizes to camelCase",),
        )

    # ── presence on each surface ─────────────────────────────────────

    def test_review_focus_carries_block(self) -> None:
        out = prompts.review_focus("Fix calc.add", _am_plugin()).text
        assert _AM_HEADER in out
        assert _AM_ENTRY in out
        # Code-owned semantics preamble travels with the block.
        assert "NOT a scope violation" in out

    def test_plan_review_focus_carries_block(self) -> None:
        out = prompts.plan_review_focus("Validate the plan", _am_plugin()).text
        assert _AM_HEADER in out
        assert _AM_ENTRY in out

    def test_plan_file_review_prompt_carries_block(self) -> None:
        out = prompts.plan_file_review_prompt(
            self._parsed_plan_with_contract(),
            "Validate the plan",
            _am_plugin(),
            project_dir="/project",
        ).text
        assert _AM_HEADER in out
        assert _AM_ENTRY in out

    def test_uncommitted_review_wire_carries_block(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # adapters.run_review builds review_focus → runtime_review_uncommitted_prompt.
        # The block from the inner focus turn must survive into the final wire.
        from pipeline.phases import adapters

        captured: dict = {}

        def _fake_invoke(agent, turn, cwd, **kwargs):
            captured["text"] = turn.text
            return '{"verdict": "APPROVED", "findings": []}'

        monkeypatch.setattr(adapters, "_invoke_turn", _fake_invoke)
        adapters.run_review(
            object(), "[review_changes] t", "/checkout", _am_plugin(),
        )
        assert _AM_HEADER in captured["text"]
        assert _AM_ENTRY in captured["text"]

    # ── typed validate_plan regression: both blocks in one turn ──────

    def test_plan_file_review_has_both_allowed_and_plan_contract(self) -> None:
        out = prompts.plan_file_review_prompt(
            self._parsed_plan_with_contract(),
            "Validate the plan",
            _am_plugin(),
            project_dir="/project",
        ).text
        assert _AM_HEADER in out
        assert "## Plan Contract" in out

    # ── byte-identity when the list is empty ─────────────────────────

    def test_empty_list_is_byte_identical_on_all_surfaces(self) -> None:
        empty = PluginConfig()
        full = _am_plugin()

        # review_focus
        assert _AM_HEADER not in prompts.review_focus("t", empty).text
        # plan_review_focus
        assert _AM_HEADER not in prompts.plan_review_focus("t", empty).text
        # plan_file_review_prompt
        plan = self._parsed_plan_with_contract()
        assert _AM_HEADER not in prompts.plan_file_review_prompt(
            plan, "t", empty, project_dir="/project",
        ).text
        # And the block IS present with a non-empty list (guards against a
        # vacuous "absent everywhere" pass).
        assert _AM_HEADER in prompts.review_focus("t", full).text

    def test_part_classification_static_project_code_owned(self) -> None:
        from pipeline.prompts.types import (
            PromptCacheScope,
            PromptLayer,
            PromptStability,
        )

        turn = prompts.review_focus("Fix calc.add", _am_plugin())
        env = turn.envelope()
        assert env is not None
        part = next(
            p for p in env.parts if p.kind == "allowed_modifications"
        )
        assert part.source == "code-owned"
        assert part.stability is PromptStability.STATIC
        assert part.cache_scope is PromptCacheScope.PROJECT
        assert part.layer is PromptLayer.CONTEXT
        # STATIC/PROJECT → it lives in the cacheable prefix, not the payload.
        assert part.id in {p.id for p in env.stable_prefix_parts}


# ── T4: per-task allowed_modifications observable in Plan Contract ────────


class TestAllowedModificationsTaskLevelWiring:
    """T4: per-task ``allowed_modifications`` aggregate into the
    ``## Plan Contract`` block under ``[<task-id>]`` tags, observable on
    the real review_focus (handler-rendered plan_contract) and the typed
    validate_plan path (plan_file_review_prompt renders the contract
    itself). Plus the union test: the project STATIC plugin block and the
    per-task TURN Plan-Contract entries both ride one review PromptTurn."""

    def _plan_with_task_entry(self):
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        # Empty top-level allowed_modifications, non-empty on one subtask.
        return ParsedPlan(
            short_summary="Stub.",
            planning_context="Context.",
            subtasks=(
                SubTask(
                    id="t9",
                    goal="bump deps",
                    allowed_modifications=("yarn.lock — derived",),
                ),
            ),
            source="json",
        )

    def test_review_focus_carries_task_tagged_entry(self) -> None:
        from pipeline.plan_contract import render_plan_contract

        plan = self._plan_with_task_entry()
        # Handler renders plan_contract then passes it to review_focus.
        contract = render_plan_contract(plan)
        out = prompts.review_focus(
            "Review changes", PluginConfig(), plan_contract=contract,
        ).text
        assert "## Plan Contract" in out
        assert "[t9] yarn.lock — derived" in out

    def test_plan_file_review_carries_task_tagged_entry(self) -> None:
        plan = self._plan_with_task_entry()
        out = prompts.plan_file_review_prompt(
            plan,
            "Validate the plan",
            PluginConfig(),
            project_dir="/project",
        ).text
        assert "## Plan Contract" in out
        assert "[t9] yarn.lock — derived" in out

    def test_project_block_and_task_entry_share_one_turn(self) -> None:
        # Union semantics: plugin-level project block (STATIC '## Allowed
        # Companion Modifications') AND per-task Plan-Contract section
        # ('Allowed companion modifications' with [task-id]) in one turn.
        from pipeline.plan_contract import render_plan_contract

        plan = self._plan_with_task_entry()
        contract = render_plan_contract(plan)
        out = prompts.review_focus(
            "Review changes", _am_plugin(), plan_contract=contract,
        ).text
        # Project-level STATIC plugin block.
        assert _AM_HEADER in out
        assert _AM_ENTRY in out
        # Per-task entry inside the turn-scoped Plan Contract.
        assert "## Plan Contract" in out
        assert "**Allowed companion modifications:**" in out
        assert "[t9] yarn.lock — derived" in out
