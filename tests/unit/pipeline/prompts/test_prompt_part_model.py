"""Unit tests for the ADR-0026 typed :class:`PromptPart` model.

The tests pin the validation rules, default-derivation behavior, and
layer-based ordering helper, then prove the extended model preserves
existing builder output for the five core phases (plan, validate_plan,
implement, review_changes, repair_changes) — i.e. M1 is a pure
metadata extension with no rendered-text drift.
"""

from __future__ import annotations

import pytest

from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
    sort_parts_by_layer,
)


class TestVolatilityValidation:
    """ADR 0026: volatile parts must declare why they are volatile;
    static + global parts must not."""

    def test_volatile_part_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="requires a volatile_reason"):
            PromptPart(
                kind="task",
                name="validate_plan",
                source="code-owned",
                body="task body",
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
            )

    def test_cache_scope_none_is_volatile_even_when_static(self) -> None:
        with pytest.raises(ValueError, match="requires a volatile_reason"):
            PromptPart(
                kind="task",
                name="validate_plan",
                source="code-owned",
                body="task body",
                stability=PromptStability.STATIC,
                cache_scope=PromptCacheScope.NONE,
            )

    def test_static_global_rejects_volatile_reason(self) -> None:
        with pytest.raises(
            ValueError, match="must not.*carry a volatile_reason",
        ):
            PromptPart(
                kind="role",
                name="systems_architect",
                source="core",
                body="role body",
                stability=PromptStability.STATIC,
                cache_scope=PromptCacheScope.GLOBAL,
                volatile_reason="should not be allowed",
            )

    def test_volatile_part_with_reason_constructs(self) -> None:
        part = PromptPart(
            kind="task",
            name="validate_plan",
            source="code-owned",
            body="task body",
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="carries reviewed file path and feedback",
        )
        assert part.volatile_reason == "carries reviewed file path and feedback"
        assert part.stability is PromptStability.TURN


class TestPartIdAndVersion:
    """ADR 0026: part ids are required (callers may rely on auto-derivation
    from kind:name) and version is preserved when supplied."""

    def test_id_defaults_to_kind_colon_name(self) -> None:
        part = PromptPart(
            kind="role",
            name="systems_architect",
            source="core",
            body="x",
        )
        assert part.id == "role:systems_architect"

    def test_explicit_id_wins_over_derivation(self) -> None:
        part = PromptPart(
            kind="contract",
            name="plan_json",
            source="code-owned",
            body="x",
            id="contract:plan_json:v3",
        )
        assert part.id == "contract:plan_json:v3"

    def test_version_field_preserved(self) -> None:
        part = PromptPart(
            kind="system_tail",
            name="review_json",
            source="code-owned",
            body="x",
            version=2,
        )
        assert part.version == 2

    def test_empty_kind_and_name_with_empty_id_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty id"):
            PromptPart(kind="", name="", source="core", body="x")


class TestLayerDefaultsAndOrdering:
    """ADR 0026: stable prefix vs turn payload is structured by
    :class:`PromptLayer`. Legacy ``kind`` values must derive a sensible
    layer so existing call sites land in the correct partition."""

    @pytest.mark.parametrize(
        ("kind", "expected_layer"),
        [
            ("role", PromptLayer.ROLE),
            ("task", PromptLayer.PHASE),
            ("format", PromptLayer.PHASE),
            ("minimal_intent", PromptLayer.PHASE),
            ("system_tail", PromptLayer.CONTRACT),
        ],
    )
    def test_default_layer_per_legacy_kind(
        self, kind: str, expected_layer: PromptLayer,
    ) -> None:
        part = PromptPart(kind=kind, name="x", source="core", body="b")
        assert part.layer is expected_layer

    def test_explicit_layer_wins(self) -> None:
        part = PromptPart(
            kind="role",
            name="systems_architect",
            source="core",
            body="x",
            layer=PromptLayer.BOOTSTRAP,
        )
        assert part.layer is PromptLayer.BOOTSTRAP

    def test_parts_sort_by_layer(self) -> None:
        # Construct parts in a deliberately scrambled order, then
        # confirm ``sort_parts_by_layer`` returns them in canonical
        # layer order: bootstrap < role < phase < contract < context <
        # turn.
        scrambled = (
            PromptPart(
                kind="task",
                name="plan",
                source="core",
                body="phase",
            ),
            PromptPart(
                kind="role",
                name="systems_architect",
                source="core",
                body="role",
            ),
            PromptPart(
                kind="system_tail",
                name="plan_json",
                source="code-owned",
                body="contract",
            ),
            PromptPart(
                kind="bootstrap",
                name="orcho_bootstrap",
                source="code-owned",
                body="bootstrap",
                layer=PromptLayer.BOOTSTRAP,
            ),
            PromptPart(
                kind="context",
                name="codemap",
                source="code-owned",
                body="context",
                layer=PromptLayer.CONTEXT,
                stability=PromptStability.RUN,
                cache_scope=PromptCacheScope.SESSION,
                volatile_reason="depends on per-run codemap",
            ),
            PromptPart(
                kind="turn",
                name="task_text",
                source="code-owned",
                body="turn",
                layer=PromptLayer.TURN,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="task text changes every round",
            ),
        )
        ordered = sort_parts_by_layer(scrambled)
        assert [p.layer for p in ordered] == [
            PromptLayer.BOOTSTRAP,
            PromptLayer.ROLE,
            PromptLayer.PHASE,
            PromptLayer.CONTRACT,
            PromptLayer.CONTEXT,
            PromptLayer.TURN,
        ]

    def test_sort_is_stable_within_layer(self) -> None:
        # Two PHASE parts must keep their relative input order.
        first_phase = PromptPart(
            kind="task", name="plan", source="core", body="A",
        )
        second_phase = PromptPart(
            kind="format", name="detailed", source="core", body="B",
        )
        ordered = sort_parts_by_layer((first_phase, second_phase))
        assert ordered == (first_phase, second_phase)


class TestFromLegacyAdapter:
    """The compatibility adapter is equivalent to a constructor call
    that lets every ADR-0026 field default. Exists so callers that
    explicitly want the legacy path read clearly at the call site."""

    def test_from_legacy_matches_default_construction(self) -> None:
        legacy = PromptPart.from_legacy(
            kind="role",
            name="systems_architect",
            source="core",
            body="role body",
        )
        direct = PromptPart(
            kind="role",
            name="systems_architect",
            source="core",
            body="role body",
        )
        assert legacy == direct

    def test_from_legacy_preserves_version(self) -> None:
        legacy = PromptPart.from_legacy(
            kind="system_tail",
            name="review_json",
            source="code-owned",
            body="tail body",
            version=2,
        )
        assert legacy.version == 2


class TestBuilderOutputCompatibility:
    """M1 is a metadata-only extension. The five core builder phases
    must still produce a non-empty composed prompt that contains the
    expected role/task anchor markers and the expected code-owned
    system-tail block markers, proving no rendered-text regression."""

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_plan_builder_renders(self, plugin: PluginConfig) -> None:
        out = prompts.plan_prompt("Fix calc.add", "/proj", plugin).text
        # ADR 0028 / M10.5 Step 2: ``TASK TO PLAN:`` header rides in
        # the typed turn_input part emitted by the builder; the
        # static plan.md method prose is verified separately.
        assert "TASK TO PLAN:" in out
        assert "Fix calc.add" in out
        assert "implementation plan for the task before any code lands" in out
        # Code-owned plan-artifact-boundary contract is always
        # appended for plan; the system-tail XML envelope must still
        # render with the expected name.
        assert 'name="plan_artifact_boundary"' in out

    def test_validate_plan_builder_renders(
        self, plugin: PluginConfig,
    ) -> None:
        out = prompts.plan_review_focus("Fix calc.add", plugin, "/proj").text
        # Reviewer gate is JSON-only; the only accepted contract surface
        # is review_json_contract.
        assert 'name="review_json"' in out
        assert out.strip()

    def test_implement_builder_renders(
        self, plugin: PluginConfig,
    ) -> None:
        out = prompts.build_prompt("Fix calc.add", "/proj", plugin).text
        # Implement composes role implementation_engineer + task build +
        # format handoff; the resulting prompt is non-empty and carries
        # at least the change-handoff system tail.
        assert out.strip()
        assert "<orcho:system-block" in out

    def test_review_changes_builder_renders(
        self, plugin: PluginConfig,
    ) -> None:
        out = prompts.review_focus("Fix calc.add", plugin, "/proj").text
        # review_changes uses review_json_contract; final_acceptance
        # would use release_json_contract — that variant is exercised
        # in builder-specific tests, here we only assert the default.
        assert 'name="review_json"' in out

    def test_repair_changes_builder_renders(
        self, plugin: PluginConfig,
    ) -> None:
        out = prompts.fix_prompt(
            "Fix calc.add",
            "Reviewer says: handle empty input.",
            "/proj",
            plugin,
        ).text
        # fix_prompt threads critique into the task body and appends
        # a change-handoff system tail; non-empty rendered prompt
        # proves the metadata extension did not break composition.
        assert out.strip()
        assert "<orcho:system-block" in out

    @pytest.mark.parametrize(
        ("factory", "args"),
        [
            (prompts.build_prompt, ("Fix calc.add", "/proj")),
            (prompts.review_focus, ("Fix calc.add",)),
            (
                prompts.fix_prompt,
                ("Fix calc.add", "Reviewer says: handle empty input.", "/proj"),
            ),
        ],
    )
    def test_downstream_builders_model_plan_tasks_part(
        self,
        plugin: PluginConfig,
        factory,
        args,
    ) -> None:
        turn = factory(
            *args,
            plugin,
            plan_contract="## Plan Contract\n\n**Goal:** Ship it.",
            plan_tasks="## Tasks\n\n## Task T1: Implement the slice.",
        )
        env = turn.envelope()
        parts = {(p.kind, p.name): p for p in env.parts}

        assert ("plan_contract", "typed_plan") in parts
        assert ("plan_tasks", "execution_plan") in parts
        assert "## Task T1" in turn.text

    def test_runtime_review_wrapper_models_plan_tasks_part(self) -> None:
        turn = prompts.runtime_review_uncommitted_prompt(
            "Review focus body.",
            project_dir="/proj",
            plan_contract="## Plan Contract\n\n**Goal:** Ship it.",
            plan_tasks="## Tasks\n\n## Task T1: Review the slice.",
        )
        env = turn.envelope()
        parts = {(p.kind, p.name): p for p in env.parts}

        assert ("plan_contract", "typed_plan") in parts
        assert ("plan_tasks", "execution_plan") in parts
        assert "## Task T1" in turn.text
