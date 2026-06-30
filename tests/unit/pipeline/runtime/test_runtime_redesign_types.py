"""type system.

Pins construction-time invariants for every new type added in
(see docs/adr/0001-pipeline-redesign.md). Each invariant gets one
targeted test — if a downstream phase ever loosens validation by
accident, these tests catch it.
"""
from __future__ import annotations

import dataclasses

import pytest

from pipeline.runtime import (
    AgentRole,
    Attachment,
    AttachmentKind,
    ChangeHandoffMode,
    EffortLevel,
    ExecutionMode,
    FailStrategy,
    FullCycleDepth,
    GateKind,
    HumanAction,
    HumanReview,
    HypothesisPrelude,
    LoopStep,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseStep,
    Profile,
    ProfileKind,
    PromptSpec,
    QualityGate,
    ReviewTiming,
    ScopedTarget,
)

# ── StrEnum bridges ───────────────────────────────────────────────────────────

class TestStrEnumValues:
    """StrEnums must serialise as raw strings (JSON-friendly) and compare
 equal to their value (.value == enum-instance) for natural use in
 config / profile JSON."""

    def test_execution_mode_string(self) -> None:
        assert ExecutionMode.LINEAR == "linear"
        # ``linear`` is the only built-in execution mode; subtask delivery is
        # selected via ``implementation_execution=subtask_dag``, not here.
        assert [m.value for m in ExecutionMode] == ["linear"]

    def test_agent_role_string(self) -> None:
        assert AgentRole.ARCHITECT == "architect"
        assert AgentRole.REVIEWER.value == "reviewer"

    def test_gate_kind_distinction(self) -> None:
        assert GateKind.COMPUTATIONAL != GateKind.INFERENTIAL

    def test_fail_strategy_complete_set(self) -> None:
        assert {s.value for s in FailStrategy} == {
            "halt", "feed_into_next", "trigger_replan", "informational",
        }

    def test_human_action_complete_set(self) -> None:
        assert {a.value for a in HumanAction} == {
            "approve", "halt", "retry", "reprompt", "edit", "skip",
        }

    def test_attachment_kind_complete_set(self) -> None:
        assert {a.value for a in AttachmentKind} == {"text", "image", "binary"}

    def test_change_handoff_mode_complete_set(self) -> None:
        assert {m.value for m in ChangeHandoffMode} == {
            "uncommitted", "commit", "commit_set",
        }


# ── QualityGate ───────────────────────────────────────────────────────────────

class TestQualityGate:
    def test_minimal_construct(self) -> None:
        g = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        assert g.kind is GateKind.COMPUTATIONAL  # default
        assert g.feed_target is None

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name is empty"):
            QualityGate(name="", on_fail=FailStrategy.HALT)

    def test_feed_into_next_requires_target(self) -> None:
        with pytest.raises(ValueError, match="feed_target required"):
            QualityGate(name="lint", on_fail=FailStrategy.FEED_INTO_NEXT)

    def test_feed_into_next_with_target_ok(self) -> None:
        g = QualityGate(
            name="lint",
            on_fail=FailStrategy.FEED_INTO_NEXT,
            feed_target="repair_changes",
        )
        assert g.feed_target == "repair_changes"

    def test_frozen(self) -> None:
        g = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        with pytest.raises(dataclasses.FrozenInstanceError):
            g.name = "x"  # type: ignore[misc]


# ── HumanReview ───────────────────────────────────────────────────────────────

class TestHumanReview:
    def test_default_actions_have_terminal(self) -> None:
        # Default tuple includes APPROVE + HALT — both terminal.
        r = HumanReview()
        assert HumanAction.APPROVE in r.actions
        assert HumanAction.HALT in r.actions

    def test_empty_actions_rejected(self) -> None:
        with pytest.raises(ValueError, match="actions cannot be empty"):
            HumanReview(actions=())

    def test_no_terminal_action_rejected(self) -> None:
        # Only RETRY/REPROMPT — would hang.
        with pytest.raises(ValueError, match="terminal"):
            HumanReview(actions=(HumanAction.RETRY, HumanAction.REPROMPT))

    def test_edit_with_before_timing_rejected(self) -> None:
        with pytest.raises(ValueError, match="EDIT incompatible"):
            HumanReview(
                timing=ReviewTiming.BEFORE,
                actions=(HumanAction.EDIT, HumanAction.APPROVE),
            )

    def test_edit_with_after_timing_ok(self) -> None:
        r = HumanReview(
            timing=ReviewTiming.AFTER,
            actions=(HumanAction.EDIT, HumanAction.APPROVE),
        )
        assert HumanAction.EDIT in r.actions

    def test_negative_retry_budget_rejected(self) -> None:
        with pytest.raises(ValueError, match="retry_budget must be ≥0"):
            HumanReview(retry_budget=-1)


# ── Attachment ────────────────────────────────────────────────────────────────

class TestAttachment:
    def test_text_with_path(self) -> None:
        a = Attachment(kind=AttachmentKind.TEXT, name="spec",
                       content_path="/p/spec.md")
        assert a.content_b64 is None

    def test_image_requires_mime(self) -> None:
        with pytest.raises(ValueError, match="mime_type required"):
            Attachment(kind=AttachmentKind.IMAGE, name="m",
                       content_path="/p/m.png")

    def test_neither_path_nor_b64_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Attachment(kind=AttachmentKind.TEXT, name="x")

    def test_both_path_and_b64_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Attachment(kind=AttachmentKind.TEXT, name="x",
                       content_path="/p/x.md", content_b64="abc")

    def test_oversize_rejected(self) -> None:
        # Triggers the size limit invariant by claiming a body well over 10 MB.
        with pytest.raises(ValueError, match="exceeds limit"):
            Attachment(
                kind=AttachmentKind.TEXT, name="big",
                content_path="/p/big.md",
                size_bytes=20 * 1024 * 1024,
            )


# ── PromptSpec ────────────────────────────────────────────────────────────────


class TestPromptSpec:
    def test_minimal_construct(self) -> None:
        spec = PromptSpec(task="code_review", role="code_reviewer")
        assert spec.format is None
        assert spec.part_names() == ("roles/code_reviewer", "tasks/code_review")

    def test_with_format(self) -> None:
        spec = PromptSpec(
            task="code_review",
            role="code_reviewer",
            format="review_findings",
        )
        assert spec.part_names() == (
            "roles/code_reviewer",
            "tasks/code_review",
            "formats/review_findings",
        )

    def test_role_optional_at_construction(self) -> None:
        """``role`` is optional at construction (transitional
 convenience for ``dataclasses.replace`` patterns), but
 ``part_names`` requires it."""
        spec = PromptSpec(task="code_review")
        assert spec.role is None
        assert spec.task == "code_review"

    def test_part_names_requires_explicit_role(self) -> None:
        """No runtime-role fallback. A spec must carry an explicit
 prompt role before it can render parts — the boundary that
 keeps prompt rendering independent of execution routing."""
        spec = PromptSpec(task="code_review")
        with pytest.raises(ValueError, match="PromptSpec.role is required"):
            spec.part_names()

    def test_explicit_prompt_role_persona_override(self) -> None:
        """A profile may set ``prompt.role`` to a non-default persona
 (e.g. ``technical_editor``); the spec renders that persona
 verbatim without consulting any runtime-role mapping."""
        spec = PromptSpec(task="docs_review", role="technical_editor")
        assert spec.part_names() == (
            "roles/technical_editor",
            "tasks/docs_review",
        )

    def test_empty_role_rejected(self) -> None:
        with pytest.raises(ValueError, match="role is empty"):
            PromptSpec(task="code_review", role="")

    def test_empty_task_rejected(self) -> None:
        with pytest.raises(ValueError, match="task is empty"):
            PromptSpec(task="", role="code_reviewer")

    def test_blank_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="format is empty"):
            PromptSpec(task="code_review", role="code_reviewer", format="")


# ── PhaseStep ─────────────────────────────────────────────────────────────────

class TestPhaseStep:
    def test_minimal_construct(self) -> None:
        s = PhaseStep(phase="plan")
        assert s.execution == "linear"  # default ExecutionMode.LINEAR.value
        assert s.prompt is None
        assert s.quality_gates == ()

    def test_empty_phase_rejected(self) -> None:
        with pytest.raises(ValueError, match="phase is empty"):
            PhaseStep(phase="")

    def test_open_string_execution(self) -> None:
        # Plugin-shipped execution mode: schema accepts arbitrary string;
        # registry lookup happens at run time (R4 — execution open string).
        s = PhaseStep(phase="plan", execution="parallel_review")
        assert s.execution == "parallel_review"

    def test_duplicate_gate_names_rejected(self) -> None:
        g = QualityGate(name="tests", on_fail=FailStrategy.HALT)
        with pytest.raises(ValueError, match="duplicate quality_gate names"):
            PhaseStep(phase="implement", quality_gates=(g, g))

    def test_phase_step_with_prompt_spec(self) -> None:
        spec = PromptSpec(task="code_review", role="code_reviewer")
        s = PhaseStep(phase="review_changes", prompt=spec)
        assert s.prompt is spec
        assert s.prompt.role == "code_reviewer"

    def test_phase_step_default_prompt_is_none(self) -> None:
        assert PhaseStep(phase="review_changes").prompt is None


# ── LoopStep clean break ──────────────────────────────────────────────────────

class TestLoopStepCleanBreak:
    """R2: LoopStep wraps PhaseStep instances, not bare phase names.
 Backwards-compat ``inner_phases`` property still readable for the
 legacy runtime walker."""

    def test_construct_from_phasesteps(self) -> None:
        steps = (PhaseStep(phase="plan"), PhaseStep(phase="validate_plan"))
        loop = LoopStep(steps=steps, until="validate_plan.approved", max_rounds=2)
        assert loop.inner_phases == ("plan", "validate_plan")  # compat view
        assert all(isinstance(s, PhaseStep) for s in loop.steps)

    def test_oscillation_default(self) -> None:
        loop = LoopStep(
            steps=(PhaseStep(phase="plan"),),
            until="plan.ok",
        )
        assert loop.oscillation_halt_after == 2

    def test_oscillation_disabled(self) -> None:
        loop = LoopStep(
            steps=(PhaseStep(phase="plan"),),
            until="plan.ok",
            oscillation_halt_after=None,
        )
        assert loop.oscillation_halt_after is None


# ── Profile (new two-axis kind × variant) ─────────────────────────────────────

class TestProfile:
    def test_full_cycle_lite(self) -> None:
        p = Profile(
            name="lite",
            kind=ProfileKind.FULL_CYCLE,
            variant=FullCycleDepth.LITE.value,
            steps=(PhaseStep(phase="plan"), PhaseStep(phase="implement")),
        )
        assert p.kind is ProfileKind.FULL_CYCLE
        assert p.variant == "lite"

    def test_scoped_review(self) -> None:
        # Profile NAME is the scoped variant ("review"); phase name is
        # the new workflow-semantic ID ("review_changes"). Two different
        # axes — see ADR 0022.
        p = Profile(
            name="review",
            kind=ProfileKind.SCOPED,
            variant=ScopedTarget.REVIEW.value,
            steps=(PhaseStep(phase="review_changes"),),
        )
        assert p.variant == "review"

    def test_custom_arbitrary_variant(self) -> None:
        # CUSTOM variant unrestricted (or None).
        p = Profile(
            name="weird",
            kind=ProfileKind.CUSTOM,
            steps=(PhaseStep(phase="plan"),),
        )
        assert p.variant is None
        assert p.change_handoff is None

    def test_change_handoff_mode(self) -> None:
        p = Profile(
            name="commit-review",
            kind=ProfileKind.CUSTOM,
            steps=(PhaseStep(phase="implement"),),
            change_handoff=ChangeHandoffMode.COMMIT,
        )
        assert p.change_handoff is ChangeHandoffMode.COMMIT

    def test_invalid_change_handoff_type_rejected(self) -> None:
        with pytest.raises(TypeError, match="change_handoff"):
            Profile(
                name="bad",
                kind=ProfileKind.CUSTOM,
                steps=(PhaseStep(phase="implement"),),
                change_handoff="commit",  # type: ignore[arg-type]
            )

    def test_full_cycle_with_invalid_variant(self) -> None:
        with pytest.raises(ValueError, match="kind=FULL_CYCLE requires variant"):
            Profile(
                name="bogus",
                kind=ProfileKind.FULL_CYCLE,
                variant="medium",  # not in FullCycleDepth
                steps=(PhaseStep(phase="plan"),),
            )

    def test_scoped_with_invalid_variant(self) -> None:
        with pytest.raises(ValueError, match="kind=SCOPED requires variant"):
            Profile(
                name="bogus",
                kind=ProfileKind.SCOPED,
                variant="random",
                steps=(PhaseStep(phase="plan"),),
            )

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name is empty"):
            Profile(
                name="",
                kind=ProfileKind.CUSTOM,
                steps=(PhaseStep(phase="plan"),),
            )

    def test_empty_steps_rejected(self) -> None:
        with pytest.raises(ValueError, match="has no steps"):
            Profile(name="empty", kind=ProfileKind.CUSTOM, steps=())

    def test_invalid_step_type_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be PhaseStep or LoopStep"):
            Profile(
                name="bad",
                kind=ProfileKind.CUSTOM,
                steps=("plan",),  # raw string at top level — not allowed
            )

    def test_loop_step_at_top_level(self) -> None:
        loop = LoopStep(
            steps=(PhaseStep(phase="plan"), PhaseStep(phase="validate_plan")),
            until="validate_plan.approved",
            max_rounds=2,
        )
        p = Profile(
            name="advanced",
            kind=ProfileKind.FULL_CYCLE,
            variant=FullCycleDepth.ADVANCED.value,
            steps=(loop, PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
        )
        assert isinstance(p.steps[0], LoopStep)

    def test_phase_step_hypothesis_prelude(self) -> None:
        prelude = HypothesisPrelude(attempts=1, format="compact")
        step = PhaseStep(phase="plan", hypothesis=prelude)
        assert step.hypothesis is prelude
        assert step.hypothesis.format == "compact"

    def test_hypothesis_prelude_rejects_bool_attempts(self) -> None:
        with pytest.raises(TypeError, match="HypothesisPrelude"):
            HypothesisPrelude(attempts=True)

    def test_hypothesis_prelude_rejects_negative_attempts(self) -> None:
        with pytest.raises(TypeError, match="HypothesisPrelude"):
            HypothesisPrelude(attempts=-1)

    def test_hypothesis_prelude_rejects_blank_format(self) -> None:
        with pytest.raises(ValueError, match="format"):
            HypothesisPrelude(attempts=1, format=" ")


# ── Round-trip composition smoke ──────────────────────────────────────────────

class TestProfileComposition:
    """Build the canonical advanced/lite/scoped/task shapes from primitives
 and verify they survive construction without surprise."""

    def test_advanced_profile_shape(self) -> None:
        p = Profile(
            name="advanced",
            kind=ProfileKind.FULL_CYCLE,
            variant="advanced",
            description="QA loop + implement + REVIEW/FIX loop",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                ),
                # Implement is a linear PhaseStep; subtask delivery is selected
                # via implementation_execution=subtask_dag, not the step.
                PhaseStep(
                    phase="implement",
                    effort=EffortLevel.HIGH,
                ),
                LoopStep(
                    steps=(
                        PhaseStep(phase="review_changes"),
                        PhaseStep(phase="repair_changes"),
                    ),
                    until="review_changes.clean",
                    max_rounds=1,
                ),
                PhaseStep(phase="final_acceptance"),
            ),
        )
        assert len(p.steps) == 4
        assert p.steps[1].execution == "linear"
        assert p.steps[1].effort is EffortLevel.HIGH

    def test_plan_scoped_profile(self) -> None:
        p = Profile(
            name="plan",
            kind=ProfileKind.SCOPED,
            variant="plan",
            description="Produce plan artifact, no implementation",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                ),
            ),
        )
        assert p.kind is ProfileKind.SCOPED
        assert p.variant == "plan"


# ──────────────────────────────────────────────────────────────────────────
# Runtime-neutrality lock (zazzy-deer): every backend the engine touches
# must satisfy IAgentRuntime, and a minimal in-test FakeRuntime that
# implements only that contract must drive the adapter functions.
# ──────────────────────────────────────────────────────────────────────────

class TestIAgentRuntimeContract:
    """Pin IAgentRuntime as the sole phase-execution contract."""

    def test_claude_agent_satisfies_iagentruntime(self) -> None:
        from agents.protocols import IAgentRuntime
        from agents.runtimes.claude import ClaudeAgent

        agent = ClaudeAgent(model="any-model", effort=None)
        assert isinstance(agent, IAgentRuntime)

    def test_codex_agent_satisfies_iagentruntime(self) -> None:
        from agents.protocols import IAgentRuntime
        from agents.runtimes.codex import CodexAgent

        agent = CodexAgent(model="any-model", effort=None)
        assert isinstance(agent, IAgentRuntime)

    def test_minimal_fake_runtime_drives_phase_adapters(self, tmp_path) -> None:
        """A runtime that implements only the IAgentRuntime surface
        (invoke + reset_session + the documented attrs) must drive
        every phase adapter — the adapters MUST NOT reach for legacy
        named methods (plan/run/review_*)."""
        from agents.protocols import IAgentRuntime
        from core.observability.prompt_trace import (
            clear_last_prompt_turn,
            take_last_prompt_turn,
        )
        from pipeline.phases.adapters import (
            run_build,
            run_fix,
            run_plan,
            run_review,
        )
        from pipeline.plugins import PluginConfig

        class FakeRuntime:
            model = "fake-model"
            session_id: str | None = None
            _followup_resume_pending: bool = False
            _last_resumed_session_id: str | None = None
            _last_followup_parent_session_id: str | None = None
            runtime: str = "fake"

            def __init__(self) -> None:
                self.invoke_calls: list[tuple[str, str, dict, object]] = []

            def invoke(self, prompt, cwd, *, mutates_artifacts=False,
                       continue_session=False, attachments=()) -> str:
                assert isinstance(prompt, str)
                assert not prompt.startswith("PromptTurn(")
                turn = take_last_prompt_turn()
                assert turn is not None
                assert turn.text == prompt
                self.invoke_calls.append((prompt, cwd, {
                    "mutates_artifacts": mutates_artifacts,
                    "continue_session": continue_session,
                    "attachments": attachments,
                }, turn))
                # Fixed sentinel — parser-shaped output is not the
                # adapter's responsibility. Parser coverage stays in
                # dedicated parser unit tests.
                return "FAKE_OUTPUT"

            def reset_session(self) -> None:
                self.session_id = None

        fake = FakeRuntime()
        assert isinstance(fake, IAgentRuntime), (
            "FakeRuntime must satisfy IAgentRuntime — the structural Protocol"
        )

        plugin = PluginConfig(name="fake-plugin")
        cwd = str(tmp_path)
        clear_last_prompt_turn()

        def assert_last_prompt_contains(*fragments: str) -> None:
            prompt = fake.invoke_calls[-1][0]
            for fragment in fragments:
                assert fragment in prompt

        # plan: read-only invocation
        plan_result = run_plan(
            fake,
            "task",
            cwd,
            plugin,
            codemap="MAP",
            prompt_prefix="PREFIX",
            prompt_suffix="\n\nSUFFIX",
        )
        assert plan_result.name == "plan"
        assert plan_result.output == "FAKE_OUTPUT"
        assert fake.invoke_calls[-1][2]["mutates_artifacts"] is False
        assert_last_prompt_contains("PREFIX", "MAP", "SUFFIX")

        # implement: write invocation
        build_result = run_build(fake, "task", cwd, plugin)
        assert build_result.name == "implement"
        assert fake.invoke_calls[-1][2]["mutates_artifacts"] is True

        # review_changes: read-only invocation
        review_result = run_review(fake, "task", cwd, plugin)
        assert review_result.name == "review_changes"
        assert fake.invoke_calls[-1][2]["mutates_artifacts"] is False

        # repair_changes: write invocation
        fix_result = run_fix(
            fake,
            "task",
            "critique",
            cwd,
            plugin,
            hybrid_codemap="HYBRID MAP",
        )
        assert fix_result.name == "repair_changes"
        assert fake.invoke_calls[-1][2]["mutates_artifacts"] is True
        assert_last_prompt_contains("HYBRID MAP")


class TestPhaseHandoffPolicy:
    """``repair_attempts``/``on_exhausted`` configure the implement-phase
    substance-repair fallback; either non-default requires an interactive
    type."""

    def test_defaults(self) -> None:
        p = PhaseHandoffPolicy()
        assert p.type is PhaseHandoffType.HUMAN_BYPASS
        assert p.repair_attempts == 0
        assert p.on_exhausted == "halt"

    def test_valid_repair_policy(self) -> None:
        p = PhaseHandoffPolicy(
            type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
            repair_attempts=1,
            on_exhausted="auto_waiver",
        )
        assert p.repair_attempts == 1
        assert p.on_exhausted == "auto_waiver"

    def test_negative_repair_attempts_rejected(self) -> None:
        with pytest.raises(ValueError, match="repair_attempts must be ≥0"):
            PhaseHandoffPolicy(
                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                repair_attempts=-1,
            )

    def test_unknown_on_exhausted_rejected(self) -> None:
        with pytest.raises(ValueError, match="on_exhausted must be one of"):
            PhaseHandoffPolicy(
                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                on_exhausted="explode",
            )

    def test_non_default_on_bypass_rejected(self) -> None:
        with pytest.raises(ValueError, match="HUMAN_BYPASS never pauses"):
            PhaseHandoffPolicy(
                type=PhaseHandoffType.HUMAN_BYPASS,
                repair_attempts=1,
            )

    def test_default_on_exhausted_on_bypass_allowed(self) -> None:
        p = PhaseHandoffPolicy(
            type=PhaseHandoffType.HUMAN_BYPASS,
            repair_attempts=0,
            on_exhausted="halt",
        )
        assert p.on_exhausted == "halt"
