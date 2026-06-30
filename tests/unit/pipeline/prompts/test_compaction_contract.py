"""M14.4 — Protected ``coding_agent_compaction`` contract.

The contract defines what a coding agent's compaction summary
must preserve so a long-running session can resume after a
compaction event without losing decisions, evidence, or
pending-work pointers (ADR 0029 §"Compaction contract").

These tests pin:

- the catalog entry exists and points at a ``SystemPromptTemplate``
  with ``kind="contract"``;
- ``REQUIRED_COMPACTION_PRESERVE_FIELDS`` is the 11-item canonical
  preserve list — drift here changes the agent-side contract and
  needs a code review;
- :func:`coding_agent_compaction_contract` renders a code-owned
  :class:`SystemPromptBlock` with ``stability=STATIC`` /
  ``cache_scope=GLOBAL`` so it can sit in the leading cache prefix
  alongside the other protected contracts;
- the rendered body's preserve list ordering matches the field
  tuple so a future writer that surfaces the rendered text can
  also iterate the same canonical order without reparsing it;
- boundary discipline: no ``coding_agent_compaction.md`` may exist
  under ``_prompts/{roles,tasks,formats}/`` — the boundary suite
  (``test_prompt_boundary.py``) owns the file-existence guard;
  here we just pin that the catalog lists the protected name.
"""
from __future__ import annotations

from pipeline.prompts.contract_templates import (
    CODING_AGENT_COMPACTION,
    REQUIRED_COMPACTION_PRESERVE_FIELDS,
    SYSTEM_PROMPT_TEMPLATES,
    SystemPromptTemplate,
)
from pipeline.prompts.contracts import coding_agent_compaction_contract
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptStability,
)


class TestCatalog:
    def test_template_is_registered_under_canonical_key(self) -> None:
        assert "coding_agent_compaction" in SYSTEM_PROMPT_TEMPLATES
        assert SYSTEM_PROMPT_TEMPLATES["coding_agent_compaction"] is (
            CODING_AGENT_COMPACTION
        )

    def test_template_is_a_contract(self) -> None:
        assert isinstance(CODING_AGENT_COMPACTION, SystemPromptTemplate)
        assert CODING_AGENT_COMPACTION.kind == "contract"
        assert CODING_AGENT_COMPACTION.name == "coding_agent_compaction"

    def test_template_has_no_slots(self) -> None:
        # The compaction contract is static today — no profile-keyed
        # substitutions. If a future revision needs runtime data
        # (e.g. a workspace-specific preserve item), promote to a
        # slot here and update the boundary tests.
        assert CODING_AGENT_COMPACTION.slots == ()


class TestPreserveFields:
    def test_required_preserve_fields_count_is_eleven(self) -> None:
        # 11 items locks the ADR-0029 compaction contract v1
        # shape. Adding / removing requires a contract version
        # bump and a downstream consumer review (lab probes,
        # future compaction primitive).
        assert len(REQUIRED_COMPACTION_PRESERVE_FIELDS) == 11

    def test_required_preserve_fields_are_unique_and_ordered(self) -> None:
        # Tuple order is part of the contract: the rendered body
        # numbers the preserve items 1..11 in this order. A
        # downstream caller iterating the tuple sees the same order
        # the agent sees in prose.
        seen: set[str] = set()
        for field in REQUIRED_COMPACTION_PRESERVE_FIELDS:
            assert field not in seen, f"duplicate preserve field: {field!r}"
            seen.add(field)

    def test_required_preserve_fields_canonical_values(self) -> None:
        assert REQUIRED_COMPACTION_PRESERVE_FIELDS == (
            "task_and_acceptance",
            "approved_plan_and_non_goals",
            "files_read_and_changed",
            "code_identifiers_and_schemas",
            "commands_tests_and_outcomes",
            "errors_that_still_matter",
            "review_findings_and_blockers",
            "verification_gaps",
            "assumptions_and_open_questions",
            "risks",
            "phase_round_surface_session_metadata",
        )


class TestRenderedBody:
    def test_rendered_body_contains_one_numbered_item_per_field(self) -> None:
        body = CODING_AGENT_COMPACTION.render()
        # Each preserve item is numbered 1..N. Iterate by index +
        # check that the numbered prefix sits in the body. We
        # don't assert exact prose wording — that may evolve — only
        # that the numeric ordering line up with the canonical
        # tuple length.
        for i, _ in enumerate(REQUIRED_COMPACTION_PRESERVE_FIELDS, 1):
            assert f"{i}." in body, (
                f"rendered body missing numbered preserve item {i}"
            )

    def test_rendered_body_mentions_compaction_and_preserve(self) -> None:
        body = CODING_AGENT_COMPACTION.render()
        lower = body.lower()
        assert "compaction" in lower
        assert "preserve" in lower

    def test_rendered_body_does_not_invent_decisions(self) -> None:
        # The contract pins a "do not invent decisions" guard so
        # an agent that compacts cannot fabricate plan / verdict
        # text not present in the source span.
        body = CODING_AGENT_COMPACTION.render().lower()
        assert "do not invent" in body


class TestFactoryBlock:
    def test_factory_returns_code_owned_system_prompt_block(self) -> None:
        block = coding_agent_compaction_contract()
        assert block.name == "coding_agent_compaction"
        assert block.kind == "contract"
        # Identical to the template's render — the factory is a
        # thin wrapper that adds versioning / cache metadata.
        assert block.body == CODING_AGENT_COMPACTION.render()

    def test_block_sits_in_global_static_cache_prefix(self) -> None:
        block = coding_agent_compaction_contract()
        # The contract is identical for every project / workspace /
        # session, so the cache-first physical layout (ADR 0028)
        # places it in the leading GLOBAL / STATIC tier alongside
        # the other protected contracts.
        assert block.stability == PromptStability.STATIC
        assert block.cache_scope == PromptCacheScope.GLOBAL

    def test_factory_is_deterministic(self) -> None:
        a = coding_agent_compaction_contract()
        b = coding_agent_compaction_contract()
        assert a.body == b.body
        assert a.name == b.name
        assert a.version == b.version


class TestBoundaryHook:
    """The full file-existence boundary check lives in
    ``test_prompt_boundary.py``; this thin guard pins the protected
    name shows up where downstream-discipline tests expect it."""

    def test_protected_name_present_in_catalog_key_form(self) -> None:
        assert "coding_agent_compaction" in SYSTEM_PROMPT_TEMPLATES
