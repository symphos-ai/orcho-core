"""M14.2 — Re-fetchability classification rules.

Pure-taxonomy tests for ``pipeline.observability.output_class``. The
classifier produces lifecycle labels per ADR 0029 §"Tool-result
clearing" but does not act on them — M14.3 owns the clearing
primitive. These tests pin:

- the four-value :class:`OutputClass` enum;
- per-rule-table semantics (every shipped phase and prompt-part
  kind has an explicit class, no silent EPHEMERAL fallback for
  known surfaces);
- the safe-default rule (unknown phase / kind / surface →
  ``EPHEMERAL``);
- compatibility with the M14.1 ``context_growth`` durable shape
  (the classifier reads ``PromptPart.kind`` / phase names — both
  primary keys already surfaced in context_growth without schema
  change);
- generic dispatch through :class:`OutputDescriptor`.
"""
from __future__ import annotations

import pytest

from pipeline.observability.output_class import (
    PHASE_CLASS_RULES,
    PROMPT_PART_CLASS_RULES,
    OutputClass,
    OutputDescriptor,
    classify_output,
    classify_phase_output,
    classify_prompt_part,
)

# ── Enum shape ───────────────────────────────────────────────────────────────


class TestEnumShape:
    """ADR 0029 fixed the four lifecycle classes. Drift here means
    the ADR contract changed and the rule tables must be reviewed."""

    def test_four_classes_present(self) -> None:
        assert {c.value for c in OutputClass} == {
            "re_fetchable",
            "persisted_artifact",
            "ephemeral",
            "decision_bearing",
        }

    def test_class_names_are_string_values(self) -> None:
        # StrEnum so values serialize cleanly through JSON
        # without enum-aware encoders.
        for c in OutputClass:
            assert isinstance(c.value, str)
            assert c.value == str(c)


# ── Rule-table coverage ──────────────────────────────────────────────────────


class TestPhaseRuleCoverage:
    """Every phase that ships under the M0-M12 + M10.5 work must
    have an explicit row. A new phase landing without a row would
    silently fall back to EPHEMERAL — safe for clearing but
    misleading for evidence consumers, so it must be a deliberate
    addition, not a default."""

    @pytest.mark.parametrize(
        "phase",
        [
            "plan", "replan", "decompose", "readonly_plan",
            "hypothesis", "validate_hypothesis",
            "validate_plan", "review_changes",
            "implement", "repair_changes",
            "final_acceptance",
            "contract_check", "compliance_check",
            "cross_plan", "cross_replan",
            "cross_validate_plan", "cross_final_acceptance",
        ],
    )
    def test_shipped_phase_has_explicit_rule(self, phase: str) -> None:
        assert phase in PHASE_CLASS_RULES

    @pytest.mark.parametrize(
        ("phase", "expected"),
        [
            # Architect outputs (accepted plan / hypothesis).
            ("plan",                   OutputClass.DECISION_BEARING),
            ("replan",                 OutputClass.DECISION_BEARING),
            ("decompose",              OutputClass.DECISION_BEARING),
            ("readonly_plan",          OutputClass.DECISION_BEARING),
            ("hypothesis",             OutputClass.DECISION_BEARING),
            # Reviewer outputs (findings / verdicts).
            ("validate_plan",          OutputClass.DECISION_BEARING),
            ("validate_hypothesis",    OutputClass.DECISION_BEARING),
            ("review_changes",         OutputClass.DECISION_BEARING),
            ("final_acceptance",       OutputClass.DECISION_BEARING),
            ("contract_check",         OutputClass.DECISION_BEARING),
            ("compliance_check",       OutputClass.DECISION_BEARING),
            # Implementer outputs (diff on disk is the artefact).
            ("implement",              OutputClass.PERSISTED_ARTIFACT),
            ("repair_changes",         OutputClass.PERSISTED_ARTIFACT),
            # Cross-project surfaces mirror single-project semantics.
            ("cross_plan",             OutputClass.DECISION_BEARING),
            ("cross_replan",           OutputClass.DECISION_BEARING),
            ("cross_validate_plan",    OutputClass.DECISION_BEARING),
            ("cross_final_acceptance", OutputClass.DECISION_BEARING),
        ],
    )
    def test_classify_phase_output(
        self, phase: str, expected: OutputClass,
    ) -> None:
        assert classify_phase_output(phase) is expected


class TestPromptPartRuleCoverage:
    """Every PromptPart kind that the M10.5 / M14.1 builders emit
    must have an explicit row. M14.3 reads this table to decide
    which payloads it may clear; a kind without an explicit rule
    falls back to EPHEMERAL (never cleared), which is safe but
    also blind — so the test forces deliberate addition."""

    @pytest.mark.parametrize(
        "kind",
        [
            # Editable composable parts.
            "role", "task", "format",
            # Code-owned / runtime-derived parts.
            "system_tail", "context", "codemap",
            "handoff_contract", "minimal_intent", "text_prefix",
            # File-backed artefact.
            "artifact",
            # Decision-bearing typed parts.
            "plan_contract", "plan_tasks", "hypothesis_suffix",
            "feedback", "turn_input",
            "reviewer_critique", "human_feedback",
            "repair_receipt", "current_review_subject",
        ],
    )
    def test_shipped_kind_has_explicit_rule(self, kind: str) -> None:
        assert kind in PROMPT_PART_CLASS_RULES

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            # Re-fetchable (regeneratable from disk / code / repo).
            ("role",              OutputClass.RE_FETCHABLE),
            ("task",              OutputClass.RE_FETCHABLE),
            ("format",            OutputClass.RE_FETCHABLE),
            ("system_tail",       OutputClass.RE_FETCHABLE),
            ("context",           OutputClass.RE_FETCHABLE),
            ("codemap",           OutputClass.RE_FETCHABLE),
            ("handoff_contract",  OutputClass.RE_FETCHABLE),
            ("minimal_intent",    OutputClass.RE_FETCHABLE),
            ("text_prefix",       OutputClass.RE_FETCHABLE),
            # Disk-backed artefact.
            ("artifact",          OutputClass.PERSISTED_ARTIFACT),
            # Decision-bearing (never drop silently).
            ("plan_contract",     OutputClass.DECISION_BEARING),
            ("plan_tasks",        OutputClass.DECISION_BEARING),
            ("hypothesis_suffix", OutputClass.DECISION_BEARING),
            ("feedback",          OutputClass.DECISION_BEARING),
            ("reviewer_critique", OutputClass.DECISION_BEARING),
            ("human_feedback",    OutputClass.DECISION_BEARING),
            ("turn_input",        OutputClass.DECISION_BEARING),
            # ADR 0066 repair-receipt protocol parts — contract-bearing,
            # must not clear as ephemeral noise.
            ("repair_receipt",         OutputClass.DECISION_BEARING),
            ("current_review_subject", OutputClass.DECISION_BEARING),
        ],
    )
    def test_classify_prompt_part(
        self, kind: str, expected: OutputClass,
    ) -> None:
        assert classify_prompt_part(kind=kind) is expected


# ── Safe-default rule ────────────────────────────────────────────────────────


class TestSafeDefault:
    """Unknown / malformed inputs classify as :data:`EPHEMERAL`. The
    M14.3 clearing primitive must not touch EPHEMERAL surfaces
    without first summarizing or persisting them, so the
    safe-by-default behaviour is "leave it alone" — the strictest
    posture, not the most permissive."""

    def test_unknown_phase_returns_ephemeral(self) -> None:
        assert classify_phase_output("future_phase_z") is OutputClass.EPHEMERAL

    def test_unknown_kind_returns_ephemeral(self) -> None:
        assert classify_prompt_part(kind="future_kind_z") is OutputClass.EPHEMERAL

    def test_empty_phase_returns_ephemeral(self) -> None:
        assert classify_phase_output("") is OutputClass.EPHEMERAL

    def test_empty_kind_returns_ephemeral(self) -> None:
        assert classify_prompt_part(kind="") is OutputClass.EPHEMERAL

    def test_whitespace_phase_returns_ephemeral(self) -> None:
        # Stripped lookup — "  plan  " resolves; "  " resolves to "".
        assert classify_phase_output("   ") is OutputClass.EPHEMERAL
        assert classify_phase_output("  plan  ") is OutputClass.DECISION_BEARING

    def test_non_string_phase_returns_ephemeral(self) -> None:
        # Defensive: a wrong-typed value (None, int, etc.) must not
        # crash the classifier — it must fall through to EPHEMERAL.
        assert classify_phase_output(None) is OutputClass.EPHEMERAL  # type: ignore[arg-type]
        assert classify_phase_output(42) is OutputClass.EPHEMERAL  # type: ignore[arg-type]

    def test_non_string_kind_returns_ephemeral(self) -> None:
        assert classify_prompt_part(kind=None) is OutputClass.EPHEMERAL  # type: ignore[arg-type]
        assert classify_prompt_part(kind=12) is OutputClass.EPHEMERAL  # type: ignore[arg-type]


# ── Generic dispatch ─────────────────────────────────────────────────────────


class TestGenericDispatch:
    def test_classify_output_routes_to_phase_branch(self) -> None:
        d = OutputDescriptor(surface="phase", name="plan")
        assert classify_output(d) is OutputClass.DECISION_BEARING

    def test_classify_output_routes_to_prompt_part_branch(self) -> None:
        d = OutputDescriptor(surface="prompt_part", name="artifact")
        assert classify_output(d) is OutputClass.PERSISTED_ARTIFACT

    def test_classify_output_passes_detail_to_prompt_part(self) -> None:
        # detail is reserved for M14.3+ sub-discrimination. The
        # classifier ignores it today but must not crash on it.
        d = OutputDescriptor(
            surface="prompt_part",
            name="text_prefix",
            detail="attachments",
        )
        assert classify_output(d) is OutputClass.RE_FETCHABLE

    def test_unknown_surface_returns_ephemeral(self) -> None:
        # Future ``tool_result`` / ``runtime_event`` surfaces will
        # ship with their own rule tables; until then unknown
        # surfaces fall back to EPHEMERAL.
        d = OutputDescriptor(surface="future_surface", name="anything")
        assert classify_output(d) is OutputClass.EPHEMERAL

    def test_empty_surface_returns_ephemeral(self) -> None:
        d = OutputDescriptor(surface="")
        assert classify_output(d) is OutputClass.EPHEMERAL

    def test_non_descriptor_returns_ephemeral(self) -> None:
        # Pass a dict by mistake — must not crash, must fall back.
        assert classify_output({"surface": "phase", "name": "plan"}) is (  # type: ignore[arg-type]
            OutputClass.EPHEMERAL
        )

    def test_descriptor_is_frozen(self) -> None:
        import dataclasses
        d = OutputDescriptor(surface="phase", name="plan")
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.name = "implement"  # type: ignore[misc]


# ── M14.1 context_growth compatibility ───────────────────────────────────────


class TestContextGrowthCompatibility:
    """M14.2 is a sibling taxonomy layer to M14.1 ``context_growth``
    — not an extension. The classifier reads ``PromptPart.kind`` and
    phase names; both are already surfaced in ``context_growth``
    payloads (phase via ``phase`` field; prompt-part kinds via
    sibling ``prompt_render.part_ids`` which carries ``kind:name``
    composite keys). No M14.1 shape change is required."""

    def test_classifier_does_not_import_context_growth_module(self) -> None:
        # Layering check: output_class should be reusable by
        # callers that do not depend on context_growth (e.g. an
        # MCP read tool that classifies a fresh prompt). It must
        # not transitively pull in context_growth.
        # NB: we inspect the already-imported module rather than
        # ``importlib.reload`` — reload would create new enum
        # instances and break ``is`` comparisons in sibling tests
        # that share the same Python process.
        import pipeline.observability.output_class as oc
        forbidden = {
            "PhaseContextGrowth",
            "extract_context_growth_traces",
            "normalize_context_growth",
        }
        assert not (set(dir(oc)) & forbidden), (
            "output_class must not import from context_growth — "
            "the two are sibling layers"
        )

    def test_part_id_kind_prefix_classifies_correctly(self) -> None:
        # M12 / M14.1 stamp ``part_session_key`` strings of the
        # form ``"kind:name@version"`` in
        # ``prompt_render.part_ids``. M14.3 will iterate those
        # ids and classify by parsing the ``kind:`` prefix.
        # M14.2 ships the classifier so M14.3 has a stable API.
        sample_part_ids = [
            "role:systems_architect@0",
            "task:implement@0",
            "format:handoff@0",
            "context:project@0",
            "system_tail:plan_artifact_boundary@1",
            "turn_input:implement_task@0",
            "feedback:replan_critique@0",
            "plan_contract:typed_plan@0",
            "artifact:validate_plan@0",
        ]
        expectations = {
            "role":          OutputClass.RE_FETCHABLE,
            "task":          OutputClass.RE_FETCHABLE,
            "format":        OutputClass.RE_FETCHABLE,
            "context":       OutputClass.RE_FETCHABLE,
            "system_tail":   OutputClass.RE_FETCHABLE,
            "turn_input":    OutputClass.DECISION_BEARING,
            "feedback":      OutputClass.DECISION_BEARING,
            "plan_contract": OutputClass.DECISION_BEARING,
            "artifact":      OutputClass.PERSISTED_ARTIFACT,
        }
        for pid in sample_part_ids:
            kind = pid.split(":", 1)[0]
            assert classify_prompt_part(kind=kind) is expectations[kind]

    def test_context_growth_phase_field_classifies_correctly(self) -> None:
        # M14.1's writer stamps ``phase`` on every context_growth
        # record. M14.3 will look up that value through
        # classify_phase_output to decide clearing eligibility.
        for phase, expected in [
            ("plan",           OutputClass.DECISION_BEARING),
            ("implement",      OutputClass.PERSISTED_ARTIFACT),
            ("review_changes", OutputClass.DECISION_BEARING),
        ]:
            assert classify_phase_output(phase) is expected


# ── No-mutation invariant ────────────────────────────────────────────────────


class TestNoMutation:
    """M14.2 brief: "M14.2 should classify, not mutate." The
    classifier API must not write to the rule tables, the
    descriptor, or any external state."""

    def test_classifier_calls_do_not_mutate_phase_rules(self) -> None:
        before = dict(PHASE_CLASS_RULES)
        classify_phase_output("plan")
        classify_phase_output("future_phase_z")
        after = dict(PHASE_CLASS_RULES)
        assert after == before

    def test_classifier_calls_do_not_mutate_prompt_part_rules(self) -> None:
        before = dict(PROMPT_PART_CLASS_RULES)
        classify_prompt_part(kind="artifact")
        classify_prompt_part(kind="future_kind_z")
        after = dict(PROMPT_PART_CLASS_RULES)
        assert after == before

    def test_repeated_calls_are_idempotent(self) -> None:
        # Pure-function expectation: the same input always returns
        # the same output, with no side effects.
        for _ in range(5):
            assert classify_phase_output("plan") is OutputClass.DECISION_BEARING
            assert classify_prompt_part(kind="artifact") is (
                OutputClass.PERSISTED_ARTIFACT
            )
