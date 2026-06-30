"""Profile JSON parser.

Pins parser invariants (well-formed input → typed Profile object;
malformed → ProfileLoadError with location-shaped message), plus
end-to-end load of each shipped profile from
``_config/pipeline_profiles_v2.json``.
"""
from __future__ import annotations

import pytest

from core.infra.paths import CONFIG_DIR as _CONFIG_DIR
from pipeline.profiles.loader import (
    ProfileLoadError,
    load_profiles_v2,
    parse_profile,
    parse_profiles,
)
from pipeline.runtime import (
    ChangeHandoffMode,
    EffortLevel,
    FailStrategy,
    GateKind,
    HumanAction,
    HumanReview,
    ImplementationExecution,
    LoopStep,
    OperatingMode,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseStep,
    ProfileKind,
    PromptSpec,
    ReviewTiming,
    SemanticProfile,
)

# ── PhaseStep parsing ─────────────────────────────────────────────────────────

class TestParsePhaseStep:
    def test_minimal(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
        })
        assert isinstance(p.steps[0], PhaseStep)
        assert p.steps[0].phase == "plan"
        assert p.steps[0].execution == "linear"  # default

    def test_full_field_set(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "implement",
                "execution": "linear",
                "skill":     "team-lead",
                "effort":    "high",
                "overrides": {"runtime": "codex"},
                "quality_gates": [
                    {"name": "tests", "kind": "computational",
                     "on_fail": "feed_into_next", "feed_target": "out"}
                ],
            }],
        })
        s = p.steps[0]
        assert s.phase == "implement"
        assert s.execution == "linear"
        assert s.skill == "team-lead"
        assert s.effort is EffortLevel.HIGH
        assert s.overrides == {"runtime": "codex"}
        assert s.quality_gates[0].name == "tests"
        assert s.quality_gates[0].on_fail is FailStrategy.FEED_INTO_NEXT

    def test_phase_required(self) -> None:
        with pytest.raises(ProfileLoadError, match="phase: required"):
            parse_profile("p", {"kind": "custom", "steps": [{}]})

    def test_phase_step_role_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan", "role": "developer"}],
            })

    def test_open_string_execution(self) -> None:
        """Custom execution mode (plugin-supplied) accepted at parse time;
 registry lookup happens at run time."""
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "review_changes", "execution": "parallel_review"}],
        })
        assert p.steps[0].execution == "parallel_review"

    def test_prompt_spec_round_trip(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "review_changes",
                "prompt": {
                    "role": "code_reviewer",
                    "task": "code_review",
                    "format": "review_findings",
                },
            }],
        })
        prompt = p.steps[0].prompt
        assert isinstance(prompt, PromptSpec)
        assert prompt.role == "code_reviewer"
        assert prompt.task == "code_review"
        assert prompt.format == "review_findings"

    def test_prompt_null_accepted(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "review_changes", "prompt": None}],
        })
        assert p.steps[0].prompt is None

    def test_prompt_role_required_when_prompt_declared(self) -> None:
        """Profile prompt blocks must name a prompt-taxonomy persona.

 Omitting the whole ``prompt`` block lets the phase builder choose its
 code-owned default. Once a profile opts into explicit prompt parts,
 there is no runtime-role fallback to fill in ``prompt.role``.
 """
        with pytest.raises(ProfileLoadError, match=r"prompt\.role"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "review_changes",
                    "prompt": {"task": "code_review", "format": "review_findings"},
                }],
            })

    def test_prompt_explicit_role_persona_override(self) -> None:
        """Explicit ``prompt.role`` survives — used as a persona override
 (e.g. ``technical_editor`` running through the ``reviewer`` runtime)."""
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "review_changes",
                "prompt": {"role": "technical_editor", "task": "docs_review"},
            }],
        })
        prompt = p.steps[0].prompt
        assert prompt.role == "technical_editor"
        assert prompt.task == "docs_review"

    def test_prompt_task_required(self) -> None:
        with pytest.raises(ProfileLoadError, match=r"prompt\.task"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "review_changes",
                    "prompt": {"role": "code_reviewer"},
                }],
            })

    def test_prompt_unknown_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "review_changes",
                    "prompt": {
                        "role": "code_reviewer",
                        "task": "code_review",
                        "fmt": "review_findings",
                    },
                }],
            })

    def test_existing_profiles_without_prompt_still_load(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "review_changes"}],
        })
        assert p.steps[0].prompt is None

    def test_phase_step_hypothesis_config(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "plan",
                "hypothesis": {"attempts": 2, "format": "compact"},
            }],
        })
        assert p.steps[0].hypothesis.attempts == 2
        assert p.steps[0].hypothesis.format == "compact"

    def test_phase_step_hypothesis_requires_attempts(self) -> None:
        with pytest.raises(ProfileLoadError, match=r"hypothesis\.attempts"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan", "hypothesis": {"format": "compact"}}],
            })

    def test_phase_step_hypothesis_rejects_boolean(self) -> None:
        with pytest.raises(ProfileLoadError, match="hypothesis"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan", "hypothesis": True}],
            })

    def test_phase_step_hypothesis_rejects_boolean_attempts(self) -> None:
        with pytest.raises(ProfileLoadError, match=r"hypothesis\.attempts"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan", "hypothesis": {"attempts": True}}],
            })

    def test_unknown_phase_step_key_rejected(self) -> None:
        """Schema typos must fail fast. Otherwise ``quality_gate`` (singular)
 would silently disable verification when starts dispatching
 v2 profiles."""
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "implement", "quality_gate": []}],
            })


# ── LoopStep parsing ──────────────────────────────────────────────────────────

class TestParseLoopStep:
    def test_minimal(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"loop": {
                "steps": [{"phase": "plan"}, {"phase": "validate_plan"}],
                "until": "validate_plan.approved",
            }}],
        })
        loop = p.steps[0]
        assert isinstance(loop, LoopStep)
        assert tuple(s.phase for s in loop.steps) == ("plan", "validate_plan")
        assert loop.until == "validate_plan.approved"
        assert loop.max_rounds == 1  # default

    def test_with_max_rounds_and_extras_key(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"loop": {
                "steps": [{"phase": "plan"}],
                "until": "plan.ok",
                "max_rounds": 5,
                "round_extras_key": "plan_round",
                "oscillation_halt_after": 3,
            }}],
        })
        loop = p.steps[0]
        assert loop.max_rounds == 5
        assert loop.round_extras_key == "plan_round"
        assert loop.oscillation_halt_after == 3

    def test_oscillation_disabled(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"loop": {
                "steps": [{"phase": "plan"}],
                "until": "plan.ok",
                "oscillation_halt_after": None,
            }}],
        })
        assert p.steps[0].oscillation_halt_after is None

    def test_loop_steps_required(self) -> None:
        with pytest.raises(ProfileLoadError, match="steps: required"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"loop": {"until": "x.y"}}],
            })

    def test_loop_until_required(self) -> None:
        with pytest.raises(ProfileLoadError, match="until"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"loop": {"steps": [{"phase": "plan"}]}}],
            })

    def test_unknown_loop_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"loop": {
                    "steps": [{"phase": "plan"}],
                    "until": "plan.ok",
                    "maxRounds": 2,
                }}],
            })


# ── QualityGate + HumanReview parsing ────────────────────────────────────────

class TestParseGate:
    def test_feed_into_next_requires_target(self) -> None:
        with pytest.raises(ValueError, match="feed_target required"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "implement",
                    "quality_gates": [
                        {"name": "lint", "on_fail": "feed_into_next"}
                    ],
                }],
            })

    def test_inferential_kind(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "review_changes",
                "quality_gates": [
                    {"name": "security_review", "kind": "inferential",
                     "on_fail": "halt"}
                ],
            }],
        })
        assert p.steps[0].quality_gates[0].kind is GateKind.INFERENTIAL

    def test_unknown_gate_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "implement",
                    "quality_gates": [
                        {"name": "tests", "on_fail": "halt", "feed_targets": "x"}
                    ],
                }],
            })


class TestParseHumanReview:
    def test_default_actions(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "plan",
                "human_review": {"timing": "after"},
            }],
        })
        review = p.steps[0].human_review
        assert isinstance(review, HumanReview)
        assert review.timing is ReviewTiming.AFTER
        # HumanReview default actions include APPROVE + HALT.
        assert HumanAction.APPROVE in review.actions

    def test_custom_actions(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "implement",
                "human_review": {
                    "timing": "after",
                    "actions": ["approve", "halt", "edit"],
                    "prompt": "Review the build output",
                },
            }],
        })
        review = p.steps[0].human_review
        assert HumanAction.EDIT in review.actions
        assert review.prompt == "Review the build output"

    def test_unknown_human_review_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "implement",
                    "human_review": {"timing": "after", "backend": "tty"},
                }],
            })


class TestParsePhaseHandoff:
    """``PhaseStep.handoff`` is generic at the loader level — actions are
    not part of the schema (runtime forms ``available_actions``) and the
    loader does no phase-name check (that's a runtime concern)."""

    def test_default_bypass_when_omitted(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "validate_plan"}],
        })
        assert p.steps[0].handoff is None

    def test_explicit_human_bypass(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "validate_plan",
                "handoff": {"type": "human_bypass"},
            }],
        })
        handoff = p.steps[0].handoff
        assert isinstance(handoff, PhaseHandoffPolicy)
        assert handoff.type is PhaseHandoffType.HUMAN_BYPASS

    def test_human_feedback_on_reject(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "validate_plan",
                "handoff": {"type": "human_feedback_on_reject"},
            }],
        })
        assert p.steps[0].handoff.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT

    def test_human_feedback_always(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "validate_plan",
                "handoff": {"type": "human_feedback_always"},
            }],
        })
        assert p.steps[0].handoff.type is PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="handoff.type"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "validate_plan",
                    "handoff": {"type": "human_telepathy"},
                }],
            })

    def test_unknown_handoff_key_rejected(self) -> None:
        """``actions`` is intentionally not in the schema — runtime forms
        ``available_actions`` from verdict and exposes them via the active
        handoff payload."""
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "validate_plan",
                    "handoff": {
                        "type": "human_feedback_on_reject",
                        "actions": ["continue"],
                    },
                }],
            })

    def test_handoff_must_be_object(self) -> None:
        with pytest.raises(ProfileLoadError, match="handoff must be an object"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "validate_plan",
                    "handoff": "human_feedback_on_reject",
                }],
            })

    def test_loader_does_not_phase_name_check(self) -> None:
        """Loader stays generic: non-bypass handoff on any phase parses
        cleanly. Runtime support matrix rejects unsupported phases at
        execution time, not here."""
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "implement",
                "handoff": {"type": "human_feedback_on_reject"},
            }],
        })
        assert p.steps[0].handoff.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT

    def test_repair_attempts_and_on_exhausted_parsed(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "implement",
                "handoff": {
                    "type": "human_feedback_on_reject",
                    "repair_attempts": 1,
                    "on_exhausted": "auto_waiver",
                },
            }],
        })
        handoff = p.steps[0].handoff
        assert handoff.repair_attempts == 1
        assert handoff.on_exhausted == "auto_waiver"

    def test_repair_fields_default_when_omitted(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "validate_plan",
                "handoff": {"type": "human_feedback_on_reject"},
            }],
        })
        handoff = p.steps[0].handoff
        assert handoff.repair_attempts == 0
        assert handoff.on_exhausted == "halt"

    def test_negative_repair_attempts_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="repair_attempts"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "implement",
                    "handoff": {
                        "type": "human_feedback_on_reject",
                        "repair_attempts": -1,
                    },
                }],
            })

    def test_unknown_on_exhausted_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="on_exhausted"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "implement",
                    "handoff": {
                        "type": "human_feedback_on_reject",
                        "on_exhausted": "explode",
                    },
                }],
            })

    def test_non_default_repair_on_bypass_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="HUMAN_BYPASS never pauses"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "implement",
                    "handoff": {
                        "type": "human_bypass",
                        "repair_attempts": 1,
                    },
                }],
            })

    def test_handoff_and_human_review_mutually_exclusive(self) -> None:
        with pytest.raises(
            ProfileLoadError,
            match="handoff and human_review are mutually exclusive",
        ):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "validate_plan",
                    "human_review": {"timing": "after"},
                    "handoff": {"type": "human_feedback_on_reject"},
                }],
            })


# ── Profile-level parsing ─────────────────────────────────────────────────────

class TestParseProfile:
    def test_full_cycle_lite(self) -> None:
        p = parse_profile("lite", {
            "kind": "full_cycle",
            "variant": "lite",
            "steps": [{"phase": "plan"}, {"phase": "implement"}],
        })
        assert p.kind is ProfileKind.FULL_CYCLE
        assert p.variant == "lite"

    def test_scoped_review(self) -> None:
        # ADR 0022: profile NAME is the scoped variant ("review"); phase
        # name is the workflow-semantic ID ("review_changes"). Two
        # different axes — phase rename does not touch profile name.
        p = parse_profile("review", {
            "kind": "scoped",
            "variant": "review",
            "steps": [{"phase": "review_changes"}],
        })
        assert p.kind is ProfileKind.SCOPED
        assert p.variant == "review"

    def test_change_handoff_optional_profile_policy(self) -> None:
        p = parse_profile("review-commits", {
            "kind": "custom",
            "change_handoff": "commit",
            "steps": [{"phase": "review_changes"}],
        })
        assert p.change_handoff is ChangeHandoffMode.COMMIT

    def test_invalid_change_handoff_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="change_handoff"):
            parse_profile("p", {
                "kind": "custom",
                "change_handoff": "branch",
                "steps": [{"phase": "review_changes"}],
            })

    def test_implementation_execution_profile_policy(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "implementation_execution": "subtask_dag",
            "steps": [{"phase": "implement"}],
        })

        assert p.implementation_execution is ImplementationExecution.SUBTASK_DAG

    def test_invalid_implementation_execution_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="implementation_execution"):
            parse_profile("p", {
                "kind": "custom",
                "implementation_execution": "sequential_subtasks",
                "steps": [{"phase": "implement"}],
            })

    def test_invalid_kind_variant_combo(self) -> None:
        with pytest.raises(ValueError, match="kind=FULL_CYCLE"):
            parse_profile("p", {
                "kind": "full_cycle",
                "variant": "review_changes",  # wrong axis
                "steps": [{"phase": "plan"}],
            })

    def test_steps_required(self) -> None:
        with pytest.raises(ProfileLoadError, match="steps"):
            parse_profile("p", {"kind": "custom"})

    def test_unknown_kind(self) -> None:
        with pytest.raises(ProfileLoadError, match="kind"):
            parse_profile("p", {"kind": "experimental",
                                "steps": [{"phase": "plan"}]})

    def test_unknown_profile_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "notes": "not part of the schema",
            })

    def test_description_must_be_string(self) -> None:
        with pytest.raises(ProfileLoadError, match="description"):
            parse_profile("p", {
                "kind": "custom",
                "description": 123,
                "steps": [{"phase": "plan"}],
            })


class TestSemanticIdentityFields:
    """Stage C: built-in profiles carry explicit semantic identity
    (``semantic_profile`` / ``default_mode`` / ``recipe_kind``). These are
    the source of semantic identity — not ``variant`` — and the loader must
    parse them onto the typed ``Profile`` so the runtime can read them.
    """

    def test_all_three_fields_parsed(self) -> None:
        p = parse_profile("feature", {
            "kind": "full_cycle",
            "variant": "advanced",
            "semantic_profile": "feature",
            "default_mode": "fast",
            "recipe_kind": "full_cycle",
            "steps": [{"phase": "plan"}, {"phase": "implement"}],
        })
        assert p.semantic_profile is SemanticProfile.FEATURE
        assert p.default_mode is OperatingMode.FAST
        assert p.recipe_kind == "full_cycle"

    def test_fields_default_to_none_when_omitted(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
        })
        assert p.semantic_profile is None
        assert p.default_mode is None
        assert p.recipe_kind is None

    def test_load_profiles_v2_surfaces_metadata_on_object(self) -> None:
        # An in-memory registry routed through parse_profiles (the same
        # parser load_profiles_v2 uses) keeps the semantic metadata on the
        # loaded Profile object all the way to the runtime.
        registry = parse_profiles({
            "complex_feature": {
                "kind": "full_cycle",
                "variant": "enterprise",
                "semantic_profile": "complex_feature",
                "default_mode": "pro",
                "recipe_kind": "full_cycle",
                "steps": [{"phase": "plan"}, {"phase": "implement"}],
            },
        })
        loaded = registry["complex_feature"]
        assert loaded.semantic_profile is SemanticProfile.COMPLEX_FEATURE
        assert loaded.default_mode is OperatingMode.PRO
        assert loaded.recipe_kind == "full_cycle"

    def test_invalid_semantic_profile_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="semantic_profile"):
            parse_profile("p", {
                "kind": "custom",
                "semantic_profile": "heavy_feature",  # Stage B draft name
                "steps": [{"phase": "plan"}],
            })

    def test_invalid_default_mode_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="default_mode"):
            parse_profile("p", {
                "kind": "custom",
                "default_mode": "team",  # historical, not a live mode
                "steps": [{"phase": "plan"}],
            })

    def test_invalid_recipe_kind_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="recipe_kind"):
            parse_profile("p", {
                "kind": "custom",
                "recipe_kind": "made_up_kind",
                "steps": [{"phase": "plan"}],
            })

    def test_recipe_kind_must_be_string(self) -> None:
        with pytest.raises(ProfileLoadError, match="recipe_kind"):
            parse_profile("p", {
                "kind": "custom",
                "recipe_kind": 7,
                "steps": [{"phase": "plan"}],
            })

    def test_variant_is_not_semantic_identity(self) -> None:
        # A built-in may still carry the legacy variant for plugin/custom
        # compatibility, but semantic identity comes from semantic_profile.
        p = parse_profile("refactor", {
            "kind": "full_cycle",
            "variant": "advanced",
            "semantic_profile": "refactor",
            "default_mode": "pro",
            "recipe_kind": "full_cycle",
            "steps": [{"phase": "plan"}, {"phase": "implement"}],
        })
        assert p.variant == "advanced"
        assert p.semantic_profile is SemanticProfile.REFACTOR


class TestParseCrossPolicy:
    """Per-step ``cross`` policy is optional schema — present for cross
    runs, absent for mono runs. The loader validates structure and
    rejects unknown keys / invalid scope values.
    """

    def test_cross_absent_stays_valid(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
        })
        assert p.steps[0].cross is None

    def test_minimal_cross_policy_parses(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan", "cross": {"scope": "global"}}],
        })
        assert p.steps[0].cross is not None
        assert p.steps[0].cross.scope.value == "global"
        assert p.steps[0].cross.handler is None

    def test_cross_policy_with_handler_parses(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{
                "phase": "plan",
                "cross": {"scope": "global", "handler": "cross_plan"},
            }],
        })
        assert p.steps[0].cross.scope.value == "global"
        assert p.steps[0].cross.handler == "cross_plan"

    def test_invalid_scope_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="scope"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan", "cross": {"scope": "bogus"}}],
            })

    def test_scope_required(self) -> None:
        with pytest.raises(ProfileLoadError, match="scope: required"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan", "cross": {}}],
            })

    def test_unknown_key_inside_cross_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "plan",
                    "cross": {"scope": "global", "weight": 7},
                }],
            })

    def test_handler_must_be_string(self) -> None:
        with pytest.raises(ProfileLoadError, match="handler"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{
                    "phase": "plan",
                    "cross": {"scope": "global", "handler": 5},
                }],
            })


class TestParseCrossGates:
    """``cross_gates`` is a top-level optional profile block describing
    policy for runner-owned terminal gates (``contract_check`` /
    ``cross_final_acceptance``). The loader validates the structure;
    consumption-time defaults handle missing entries.
    """

    def test_missing_cross_gates_yields_empty_mapping(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
        })
        assert dict(p.cross_gates) == {}

    def test_missing_block_resolves_to_disabled(self) -> None:
        """Missing ``cross_gates`` block ≡ both gates disabled. Profile
        authors opt INTO cross gates explicitly; the runtime never
        imposes them on profiles that don't ask."""
        from pipeline.cross_project.profile_projection import (
            get_cross_gate_policy,
        )
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
        })
        cc = get_cross_gate_policy(p, "contract_check")
        assert cc.enabled is False
        cfa = get_cross_gate_policy(p, "cross_final_acceptance")
        assert cfa.enabled is False

    def test_partial_block_disables_missing_gate(self) -> None:
        """Fallback is per-gate, not per-block. A profile that names
        only one of the two keys leaves the other disabled."""
        from pipeline.cross_project.profile_projection import (
            get_cross_gate_policy,
        )
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
            "cross_gates": {
                "contract_check": {
                    "enabled": True,
                    "mode": "artifact_bundle",
                    "run": "always",
                    "on_skip": "block",
                },
            },
        })
        cc = get_cross_gate_policy(p, "contract_check")
        assert cc.enabled is True
        cfa = get_cross_gate_policy(p, "cross_final_acceptance")
        assert cfa.enabled is False

    def test_explicit_policy_overrides_defaults(self) -> None:
        from pipeline.cross_project.profile_projection import (
            get_cross_gate_policy,
        )
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
            "cross_gates": {
                "contract_check": {
                    "enabled": True,
                    "mode": "artifact_bundle",
                    "run": "manual_confirm",
                    "on_skip": "allow_with_gap",
                },
                "cross_final_acceptance": {
                    "enabled": True,
                    "run": "auto",
                },
            },
        })
        cc = get_cross_gate_policy(p, "contract_check")
        assert cc.run.value == "manual_confirm"
        assert cc.on_skip.value == "allow_with_gap"
        cfa = get_cross_gate_policy(p, "cross_final_acceptance")
        assert cfa.run.value == "auto"

    def test_unknown_gate_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown gate keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {"contract_chek": {"enabled": False}},
            })

    def test_unknown_run_value_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="contract_check.run"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "contract_check": {"run": "maybe"},
                },
            })

    def test_unknown_on_skip_value_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="on_skip"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "contract_check": {"on_skip": "soft"},
                },
            })

    def test_contract_check_mode_artifact_bundle_accepted(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
            "cross_gates": {
                "contract_check": {"mode": "artifact_bundle"},
            },
        })
        assert p.cross_gates["contract_check"].mode == "artifact_bundle"

    def test_contract_check_mode_repo_review_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="mode"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "contract_check": {"mode": "repo_review"},
                },
            })

    def test_cross_final_acceptance_mode_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="not applicable"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "cross_final_acceptance": {"mode": "artifact_bundle"},
                },
            })

    def test_cross_final_acceptance_manual_confirm_rejected(self) -> None:
        with pytest.raises(
            ProfileLoadError, match="manual_confirm",
        ):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "cross_final_acceptance": {"run": "manual_confirm"},
                },
            })

    def test_unknown_per_gate_key_rejected(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "contract_check": {"weight": 7},
                },
            })

    def test_enabled_must_be_bool(self) -> None:
        with pytest.raises(ProfileLoadError, match="enabled"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": {
                    "contract_check": {"enabled": "yes"},
                },
            })

    def test_cross_gates_must_be_object(self) -> None:
        with pytest.raises(ProfileLoadError, match="cross_gates"):
            parse_profile("p", {
                "kind": "custom",
                "steps": [{"phase": "plan"}],
                "cross_gates": [],
            })

    def test_profile_cross_gates_is_immutable(self) -> None:
        p = parse_profile("p", {
            "kind": "custom",
            "steps": [{"phase": "plan"}],
            "cross_gates": {
                "contract_check": {"run": "never"},
            },
        })
        with pytest.raises(TypeError):
            p.cross_gates["contract_check"] = None  # type: ignore[index]


class TestParseProfiles:
    def test_skips_comment_keys(self) -> None:
        out = parse_profiles({
            "_comment": "ignored",
            "_comment_aux": "also ignored",
            "lite": {"kind": "full_cycle", "variant": "lite",
                     "steps": [{"phase": "plan"}]},
        })
        assert "_comment" not in out
        assert "lite" in out

    def test_top_level_must_be_object(self) -> None:
        with pytest.raises(ProfileLoadError, match="top-level"):
            parse_profiles([])  # type: ignore[arg-type]


# ── Integration: shipped v2 JSON loads end-to-end ────────────────────────────

V2_PATH = _CONFIG_DIR / "pipeline_profiles_v2.json"


class TestShippedProfilesV2:
    def test_file_exists(self) -> None:
        assert V2_PATH.exists(), f"shipped v2 profiles not found: {V2_PATH}"

    def test_parses_without_error(self) -> None:
        """Loader smoke: the shipped catalogue loads end-to-end and
        produces at least one profile. Catches JSON corruption /
        loader regression / unknown-key validation failure. Does NOT
        pin the catalogue's exact name set — that's a tuning concern
        that lives in the JSON, not the test."""
        profiles = load_profiles_v2(V2_PATH)
        assert profiles, "shipped v2 catalogue must contain ≥1 profile"

    def test_descriptions_non_empty(self) -> None:
        for p in load_profiles_v2(V2_PATH).values():
            assert p.description, f"profile {p.name} has empty description"

    def test_feature_profile_keeps_strict_terminal_cross_gates(self) -> None:
        # ``feature`` is the full_cycle work kind migrated from the old
        # ``advanced`` recipe and keeps both terminal cross gates strict.
        from pipeline.cross_project.profile_projection import (
            get_cross_gate_policy,
        )

        feature = load_profiles_v2(V2_PATH)["feature"]
        contract_check = get_cross_gate_policy(feature, "contract_check")
        assert contract_check.enabled is True
        assert contract_check.run.value == "always"
        assert contract_check.on_skip.value == "block"
        assert contract_check.mode == "artifact_bundle"

        final_acceptance = get_cross_gate_policy(
            feature,
            "cross_final_acceptance",
        )
        assert final_acceptance.enabled is True
        assert final_acceptance.run.value == "always"
        assert final_acceptance.on_skip.value == "block"

    def test_feature_profile_uses_subtask_dag_implementation(self) -> None:
        feature = load_profiles_v2(V2_PATH)["feature"]

        assert feature.implementation_execution is (
            ImplementationExecution.SUBTASK_DAG
        )

    def test_cheap_and_scoped_profiles_use_direct_checkout(self) -> None:
        profiles = load_profiles_v2(V2_PATH)

        # Focused / cheap work kinds + the internal task profile work in the
        # direct checkout.
        for name in (
            "small_task", "planning", "research",
            "delivery_audit", "code_review", "task",
        ):
            assert profiles[name].worktree_isolation == "off"

        # Full_cycle work kinds keep the global per_run default (None).
        assert profiles["feature"].worktree_isolation is None
        assert profiles["complex_feature"].worktree_isolation is None
        assert profiles["refactor"].worktree_isolation is None
        assert profiles["migration"].worktree_isolation is None
        assert profiles["correction"].worktree_isolation is None

    def test_correction_is_internal_without_planning_phases(self) -> None:
        """``correction`` is the internal system follow-up profile (ADR 0085):
        it is flagged ``internal`` (variant ``correction``) and skips the
        planning phases — the change already has a plan from the parent run,
        so neither ``plan`` nor ``validate_plan`` may appear among its steps.
        Reads the loaded profile, so reintroducing a planning phase (or
        dropping the internal flag) fails here."""
        correction = load_profiles_v2(V2_PATH)["correction"]

        assert correction.internal is True
        assert correction.variant == "correction"

        phases: set[str] = set()
        for entry in correction.steps:
            if isinstance(entry, LoopStep):
                phases.update(inner.phase for inner in entry.steps)
            else:
                phases.add(entry.phase)

        assert "plan" not in phases
        assert "validate_plan" not in phases

    def test_phase_handoff_default_matrix(self) -> None:
        """Loader honors whatever ``handoff`` shape ``pipeline_profiles_v2.json``
        declares for each built-in profile's validate_plan.

        This used to hard-code expected values per profile name, which
        coupled the test to a config file that operators legitimately
        edit. The check is now config-driven: read the JSON, derive the
        expected handoff type per profile, then assert the loader
        surfaces the same type on the parsed ``Profile``. Profiles
        without a validate_plan step (review, task) are skipped — they
        have no handoff to declare. Profiles that omit the field
        altogether are treated as ``human_bypass`` per the loader
        contract.
        """
        import json

        from pipeline.runtime import (
            LoopStep,
            PhaseHandoffType,
            PhaseStep,
            Profile,
        )

        def _expected_handoff_from_json(
            raw_profile: dict[str, object],
        ) -> PhaseHandoffType | None:
            """Walk the JSON profile shape and pull the validate_plan
            handoff type, or None if no validate_plan step is declared."""
            for entry in raw_profile.get("steps", []) or []:
                if not isinstance(entry, dict):
                    continue
                # Loop step — descend into ``loop.steps``.
                loop_block = entry.get("loop")
                if isinstance(loop_block, dict):
                    inner_steps = loop_block.get("steps") or []
                    for inner in inner_steps:
                        if (
                            isinstance(inner, dict)
                            and inner.get("phase") == "validate_plan"
                        ):
                            handoff = inner.get("handoff")
                            if isinstance(handoff, dict) and handoff.get("type"):
                                return PhaseHandoffType(handoff["type"])
                            return PhaseHandoffType.HUMAN_BYPASS
                # Plain phase step at top level.
                if entry.get("phase") == "validate_plan":
                    handoff = entry.get("handoff")
                    if isinstance(handoff, dict) and handoff.get("type"):
                        return PhaseHandoffType(handoff["type"])
                    return PhaseHandoffType.HUMAN_BYPASS
            return None

        def _validate_plan_handoff_type(profile: Profile) -> PhaseHandoffType | None:
            for entry in profile.steps:
                if isinstance(entry, LoopStep):
                    for inner in entry.steps:
                        if (
                            isinstance(inner, PhaseStep)
                            and inner.phase == "validate_plan"
                        ):
                            if inner.handoff is None:
                                return PhaseHandoffType.HUMAN_BYPASS
                            return inner.handoff.type
                elif (
                    isinstance(entry, PhaseStep)
                    and entry.phase == "validate_plan"
                ):
                    if entry.handoff is None:
                        return PhaseHandoffType.HUMAN_BYPASS
                    return entry.handoff.type
            return None

        raw = json.loads(V2_PATH.read_text())
        # Profiles live as top-level keys (the semantic work kinds plus the
        # internal ``task`` / ``correction``). Skip underscore keys
        # (``_comment``) that the JSON uses for inline docs.
        raw_profiles = {
            name: spec
            for name, spec in raw.items()
            if not name.startswith("_") and isinstance(spec, dict)
        }
        profiles = load_profiles_v2(V2_PATH)

        for name, raw_profile in raw_profiles.items():
            expected = _expected_handoff_from_json(raw_profile)
            actual = _validate_plan_handoff_type(profiles[name])
            assert expected == actual, (
                f"profile {name!r}: JSON declares "
                f"validate_plan.handoff={expected}, loader surfaced {actual}"
            )
