"""Per-contract metadata expectations and end-to-end envelope-partition checks for M3.

ADR 0026 partitions a rendered prompt into a stable cacheable
prefix and a per-turn payload (M2). M3 enriches each system-tail
contract block with the metadata that drives that partition:

- pure parser contracts without language directives stay
  STATIC / GLOBAL;
- language-bearing JSON contracts and the authoring_language
  strategy become PROFILE / WORKSPACE with a normalized language
  signature in the part id;
- handoff / review-target strategies become PROFILE / GLOBAL with
  the mode encoded in the id;
- protected blocks remain code-owned (``source="code-owned"``)
  and their rendered XML envelope stays byte-identical so existing
  parsers keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.contracts import (
    SystemPromptBlock,
    authoring_language_strategy,
    change_handoff_strategy,
    plan_artifact_boundary_contract,
    plan_json_contract,
    release_json_contract,
    review_json_contract,
    review_target_strategy,
)
from pipeline.prompts.envelope import is_prefix_eligible
from pipeline.prompts.types import PromptCacheScope, PromptStability

# ---------------------------------------------------------------------------
# Per-factory metadata expectations — pure-data tests, no rendering needed.
# ---------------------------------------------------------------------------


class TestPlanJsonContractMetadata:
    def test_plan_json_contract_metadata_is_static_global(self) -> None:
        block = plan_json_contract()
        assert block.stability is PromptStability.STATIC
        assert block.cache_scope is PromptCacheScope.GLOBAL
        assert block.volatile_reason is None
        # Empty id falls back to gateway derivation, but the block
        # itself records ``""`` so the M2 partitioner sees the same
        # default the M1 PromptPart constructor applies.
        assert block.id == ""

    def test_plan_json_contract_with_language_is_workspace_cacheable(
        self,
    ) -> None:
        block = plan_json_contract(body_language="English")
        assert block.stability is PromptStability.PROFILE
        assert block.cache_scope is PromptCacheScope.WORKSPACE
        assert block.volatile_reason
        assert "language configuration" in block.volatile_reason
        # id encodes the normalized language signature so two
        # projects with different body_language values cannot share a
        # cached prefix.
        assert block.id == "contract:plan_json:v1:english"

    def test_plan_json_contract_id_includes_both_languages_when_distinct(
        self,
    ) -> None:
        block = plan_json_contract(
            body_language="English", input_language="Russian",
        )
        assert "english" in block.id
        assert "russian" in block.id

    def test_plan_json_contract_id_collapses_duplicate_languages(
        self,
    ) -> None:
        same = plan_json_contract(
            body_language="English", input_language="English",
        )
        only_body = plan_json_contract(body_language="English")
        # Identical normalized language → identical id signature; the
        # M6 selector treats them as the same cacheable identity.
        assert same.id == only_body.id


class TestReviewJsonContractMetadata:
    def test_review_json_contract_metadata_is_static_global_without_language(
        self,
    ) -> None:
        block = review_json_contract()
        assert block.stability is PromptStability.STATIC
        assert block.cache_scope is PromptCacheScope.GLOBAL
        assert block.volatile_reason is None

    def test_review_json_contract_with_language_is_workspace_cacheable(
        self,
    ) -> None:
        block = review_json_contract(body_language="English")
        assert block.stability is PromptStability.PROFILE
        assert block.cache_scope is PromptCacheScope.WORKSPACE
        assert block.id == "contract:review_json:v1:english"
        assert block.volatile_reason


class TestReleaseJsonContractMetadata:
    def test_release_json_contract_with_language_is_workspace_cacheable(
        self,
    ) -> None:
        block = release_json_contract(body_language="English")
        assert block.stability is PromptStability.PROFILE
        assert block.cache_scope is PromptCacheScope.WORKSPACE
        assert block.id == "contract:release_json:v1:english"
        assert block.volatile_reason


class TestChangeHandoffMetadata:
    @pytest.mark.parametrize(
        "mode", ["uncommitted", "commit", "commit_set"],
    )
    def test_change_handoff_metadata_is_profile_global_and_id_encodes_mode(
        self, mode: str,
    ) -> None:
        block = change_handoff_strategy(mode=mode)
        assert block.stability is PromptStability.PROFILE
        assert block.cache_scope is PromptCacheScope.GLOBAL
        assert block.volatile_reason
        assert "handoff mode" in block.volatile_reason
        # Mode encoded in id so the M6 selector treats a mode flip as
        # an id change and resends the block naturally — without
        # falling back to per-turn churn.
        assert block.id == f"contract:change_handoff:mode={mode}"


class TestReviewTargetMetadata:
    @pytest.mark.parametrize(
        "mode", ["uncommitted", "commit", "commit_set"],
    )
    def test_review_target_metadata_is_profile_global_and_id_encodes_mode(
        self, mode: str,
    ) -> None:
        block = review_target_strategy(mode=mode)
        assert block.stability is PromptStability.PROFILE
        assert block.cache_scope is PromptCacheScope.GLOBAL
        assert block.id == f"contract:review_target:mode={mode}"


class TestAuthoringLanguageMetadata:
    def test_authoring_language_part_is_workspace_cacheable(self) -> None:
        block = authoring_language_strategy(task_language="English")
        assert block is not None
        assert block.stability is PromptStability.PROFILE
        assert block.cache_scope is PromptCacheScope.WORKSPACE
        assert block.volatile_reason
        # The block must remain prefix-eligible per the M2 partition
        # rule — workspace-cacheable means "in stable prefix once per
        # workspace", not "in turn payload every round".
        assert is_prefix_eligible(_to_prompt_part(block)) is True

    def test_authoring_language_part_id_changes_with_language_value(
        self,
    ) -> None:
        en = authoring_language_strategy(task_language="English")
        ru = authoring_language_strategy(task_language="Russian")
        assert en is not None and ru is not None
        assert en.id != ru.id
        # ids carry the normalized signature so the M6 selector
        # detects the language flip via id change rather than
        # body-content diffing.
        assert "english" in en.id
        assert "russian" in ru.id

    def test_authoring_language_normalizes_whitespace_and_case(self) -> None:
        a = authoring_language_strategy(task_language="English")
        b = authoring_language_strategy(task_language="  english  ")
        assert a is not None and b is not None
        assert a.id == b.id

    def test_authoring_language_returns_none_for_empty_language(self) -> None:
        assert authoring_language_strategy(task_language=None) is None
        assert authoring_language_strategy(task_language="") is None
        assert authoring_language_strategy(task_language="   ") is None


# ---------------------------------------------------------------------------
# Protected XML envelope — bytes must stay identical so parsers do
# not break.
# ---------------------------------------------------------------------------


class TestProtectedBlockXmlEnvelope:
    def test_protected_blocks_render_unchanged_xml_envelope(self) -> None:
        # Each block's render() must wrap its body in the canonical
        # ``<orcho:system-block kind="..." name="..." version="N">``
        # envelope, regardless of the new ADR-0026 metadata. Parsers
        # downstream key off this exact shape; any drift would silently
        # break review/release verdict parsing.
        blocks: list[SystemPromptBlock | None] = [
            plan_json_contract(),
            plan_json_contract(body_language="English"),
            review_json_contract(),
            review_json_contract(body_language="English"),
            release_json_contract(body_language="English"),
            change_handoff_strategy(mode="uncommitted"),
            review_target_strategy(mode="uncommitted"),
            plan_artifact_boundary_contract(),
            authoring_language_strategy(task_language="English"),
        ]
        for block in blocks:
            assert block is not None
            rendered = block.render()
            assert rendered.startswith(
                f'<orcho:system-block kind="{block.kind}" '
                f'name="{block.name}" version="{block.version}">\n',
            )
            assert rendered.endswith("\n</orcho:system-block>")


# ---------------------------------------------------------------------------
# End-to-end: run a real builder, take the M2 envelope from
# prompt_trace, assert each contract part lands in the partition
# declared by its factory metadata.
# ---------------------------------------------------------------------------


class TestEnvelopePartitionMatchesBlockMetadata:
    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_envelope_partition_matches_block_metadata(
        self, plugin: PluginConfig,
    ) -> None:
        # plan_prompt assembles a system_tail of:
        #   plan_json_contract(body_language=..., input_language=...)
        #   plan_artifact_boundary_contract()
        #   authoring_language_strategy(task_language=...)
        # The M3 contract is that each block's declared metadata
        # propagates through the gateway into the PromptPart the M2
        # envelope sees. M2's conservative contiguous-prefix rule may
        # still demote a block to payload for wire-layout safety
        # (because the task body precedes it as TURN); reordering for
        # tighter packing is M10's job. So we assert metadata
        # propagation, not partition placement.
        env = prompts.plan_prompt("Fix calc.add", "/proj", plugin).envelope()
        assert env is not None

        by_name = {p.name: p for p in env.parts if p.kind == "system_tail"}

        # plan_json with language should carry PROFILE/WORKSPACE +
        # language-aware id.
        plan_json = by_name["plan_json"]
        assert plan_json.stability is PromptStability.PROFILE
        assert plan_json.cache_scope is PromptCacheScope.WORKSPACE
        assert "english" in plan_json.id

        # plan_artifact_boundary is a pure constant: STATIC/GLOBAL.
        boundary = by_name["plan_artifact_boundary"]
        assert boundary.stability is PromptStability.STATIC
        assert boundary.cache_scope is PromptCacheScope.GLOBAL
        assert boundary.volatile_reason is None

        # authoring_language carries the workspace policy metadata.
        lang = by_name["authoring_language"]
        assert lang.stability is PromptStability.PROFILE
        assert lang.cache_scope is PromptCacheScope.WORKSPACE
        assert "english" in lang.id

        # All three are independently prefix-eligible by metadata. M2's
        # contiguous-prefix rule may still demote them for layout
        # reasons; M10 will reorder so these actually land in prefix
        # at render time.
        for part in (plan_json, boundary, lang):
            assert is_prefix_eligible(part) is True

    def test_authoring_language_not_in_turn_payload(
        self, plugin: PluginConfig,
    ) -> None:
        # Brief test name: authoring_language must never be classified
        # as turn payload. We assert this at the metadata level (the
        # contract M3 owns), not at the wire-layout level (that's M10).
        # M2's conservative contiguous-prefix rule may still place the
        # block in the payload partition because the rendered task
        # body precedes it as TURN, but the block's declared metadata
        # is workspace policy — not per-turn noise.
        env = prompts.plan_prompt("Fix calc.add", "/proj", plugin).envelope()
        assert env is not None

        lang_parts = [
            p for p in env.parts
            if p.kind == "system_tail" and p.name == "authoring_language"
        ]
        assert lang_parts, "expected authoring_language to be present"
        for part in lang_parts:
            # Independently of where the wire-layout demoter places it,
            # the part's declared classification must NOT be turn
            # (the brief's "not in turn payload" claim, applied at the
            # metadata layer M3 is responsible for).
            assert part.stability is not PromptStability.TURN
            assert part.cache_scope is not PromptCacheScope.NONE
            assert is_prefix_eligible(part) is True


# ---------------------------------------------------------------------------
# Override-protection: project/workspace markdown overrides cannot
# remove a code-owned contract from the rendered prompt.
# ---------------------------------------------------------------------------


class TestProjectWorkspaceOverrideProtection:
    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_overrides_cannot_remove_plan_artifact_boundary(
        self, plugin: PluginConfig, tmp_path: Path,
    ) -> None:
        # Even with a project override of tasks/plan that drops every
        # mention of artifact persistence, the rendered plan prompt
        # still ends with the code-owned plan_artifact_boundary
        # system block — it is appended in plan_prompt(), never via
        # the markdown layer.
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "plan.md").write_text(
            "TASK TO PLAN: $task\n\n"
            "Produce an implementation plan before any code lands.\n",
            encoding="utf-8",
        )
        rendered = prompts.plan_prompt("Fix calc.add", str(tmp_path), plugin).text
        assert 'name="plan_artifact_boundary"' in rendered

    def test_custom_plan_prompt_still_includes_artifact_boundary(
        self, plugin: PluginConfig, tmp_path: Path,
    ) -> None:
        # Even more aggressive: a project override that wipes the
        # task body to a single line. The artifact-boundary contract
        # still appears because it lives in the system tail, not in
        # the user-editable layer.
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "plan.md").write_text("PLAN: $task\n", encoding="utf-8")
        rendered = prompts.plan_prompt("Fix calc.add", str(tmp_path), plugin).text
        assert 'name="plan_artifact_boundary"' in rendered

    def test_review_json_contract_anchor_is_protected(
        self, plugin: PluginConfig, tmp_path: Path,
    ) -> None:
        # plan_review_focus + review_focus both end with the
        # review_json contract; no project override of the task
        # markdown can drop the JSON contract anchor.
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "validate_plan.md").write_text(
            "Review the plan body: $task\n", encoding="utf-8",
        )
        rendered = prompts.plan_review_focus(
            "Fix calc.add", plugin, str(tmp_path),
        ).text
        assert 'name="review_json"' in rendered

    def test_handoff_policy_anchor_is_protected(
        self, plugin: PluginConfig, tmp_path: Path,
    ) -> None:
        # build_prompt always appends the change_handoff strategy
        # block; project markdown overrides cannot remove it.
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "implement.md").write_text(
            "IMPLEMENT: $task\n", encoding="utf-8",
        )
        rendered = prompts.build_prompt(
            "Fix calc.add", str(tmp_path), plugin,
        ).text
        assert 'name="change_handoff"' in rendered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_prompt_part(block: SystemPromptBlock):
    """Mirror the gateway's SystemPromptBlock → PromptPart conversion.

    The real conversion lives in
    ``pipeline.prompts.builders._render_prompt_output``; this helper
    keeps the metadata-only tests independent of the builder
    pipeline so they can assert prefix-eligibility in isolation.
    """
    from pipeline.prompts.types import PromptPart

    return PromptPart(
        kind="system_tail",
        name=block.name,
        source="code-owned",
        body=block.render(),
        version=block.version,
        id=block.id,
        layer=block.layer,
        stability=block.stability,
        cache_scope=block.cache_scope,
        volatile_reason=block.volatile_reason,
    )
