"""Tests for PromptTurn, PromptSegment, PromptTurnEditor, and assemble_cache_first_segments.

Covers:
- Segment invariants
- PromptTurn projections (text, parts, trace_view)
- PromptTurnEditor (prepend/append/inject_mid/build)
- render_selected delta parity
- assemble_cache_first_segments byte-parity with old assembler contract
- hypothesis_suffix_part factory (Bug 3 regression)
"""
from __future__ import annotations

import pytest

from pipeline.prompts.composer import assemble_cache_first_segments
from pipeline.prompts.turn import (
    PromptSegment,
    PromptTraceView,
    PromptTurn,
    PromptTurnEditor,
    hypothesis_suffix_part,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _static_part(kind: str, name: str, body: str) -> PromptPart:
    return PromptPart(kind=kind, name=name, source="core", body=body)


def _volatile_part(kind: str, name: str, body: str) -> PromptPart:
    return PromptPart(
        kind=kind,
        name=name,
        source="code-owned",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="per-turn test part",
    )


# ---------------------------------------------------------------------------
# PromptSegment invariants
# ---------------------------------------------------------------------------


class TestPromptSegmentInvariants:
    def test_non_empty_body_in_text_exactly_once(self) -> None:
        part = _static_part("role", "architect", "You are an architect.")
        seg = PromptSegment(text="You are an architect.", part=part, segment_id="s1")
        assert seg.text == "You are an architect."

    def test_non_empty_body_must_appear_exactly_once_raises_if_zero(self) -> None:
        part = _static_part("role", "architect", "body text")
        with pytest.raises(ValueError, match="exactly once"):
            PromptSegment(text="different text", part=part, segment_id="s1")

    def test_non_empty_body_must_appear_exactly_once_raises_if_twice(self) -> None:
        part = _static_part("role", "architect", "AB")
        with pytest.raises(ValueError, match="exactly once"):
            PromptSegment(text="AB AB", part=part, segment_id="s1")

    def test_empty_body_requires_empty_text(self) -> None:
        part = _static_part("role", "empty", "")
        seg = PromptSegment(text="", part=part, segment_id="s1")
        assert seg.text == ""

    def test_empty_body_with_non_empty_text_raises(self) -> None:
        part = _static_part("role", "empty", "")
        with pytest.raises(ValueError, match="empty-body part must have text=''"):
            PromptSegment(text="oops", part=part, segment_id="s1")


# ---------------------------------------------------------------------------
# PromptTurn projections
# ---------------------------------------------------------------------------


class TestPromptTurnProjections:
    def test_text_joins_segment_texts(self) -> None:
        p1 = _static_part("role", "r", "Role body")
        p2 = _static_part("task", "t", "Task body")
        s1 = PromptSegment(text="Role body", part=p1, segment_id="0")
        s2 = PromptSegment(text="\n\nTask body", part=p2, segment_id="1")
        turn = PromptTurn(segments=(s1, s2))
        assert turn.text == "Role body\n\nTask body"

    def test_parts_returns_part_sequence(self) -> None:
        p1 = _static_part("role", "r", "A")
        p2 = _static_part("task", "t", "B")
        s1 = PromptSegment(text="A", part=p1, segment_id="0")
        s2 = PromptSegment(text="\n\nB", part=p2, segment_id="1")
        turn = PromptTurn(segments=(s1, s2))
        assert turn.parts == (p1, p2)

    def test_trace_view_text_equals_turn_text(self) -> None:
        p1 = _static_part("role", "r", "Role")
        s1 = PromptSegment(text="Role", part=p1, segment_id="0")
        turn = PromptTurn(segments=(s1,))
        tv = turn.trace_view()
        assert isinstance(tv, PromptTraceView)
        assert tv.text == turn.text
        assert tv.segments == (s1,)

    def test_empty_turn_has_empty_text(self) -> None:
        turn = PromptTurn(segments=())
        assert turn.text == ""
        assert turn.parts == ()

    def test_duplicate_segment_id_rejected(self) -> None:
        # segment_id is the positional identity within a turn (parts may
        # repeat, segment_id may not).
        p1 = _static_part("role", "r", "A")
        p2 = _static_part("task", "t", "B")
        s1 = PromptSegment(text="A", part=p1, segment_id="dup")
        s2 = PromptSegment(text="\n\nB", part=p2, segment_id="dup")
        with pytest.raises(ValueError, match="duplicate segment_id"):
            PromptTurn(segments=(s1, s2))

    def test_repeated_part_with_unique_segment_ids_allowed(self) -> None:
        # Same part object in two segments is fine as long as segment_id differs.
        p = _static_part("role", "r", "A")
        s1 = PromptSegment(text="A", part=p, segment_id="0")
        s2 = PromptSegment(text="\n\nA", part=p, segment_id="1")
        turn = PromptTurn(segments=(s1, s2))
        assert turn.parts == (p, p)


# ---------------------------------------------------------------------------
# PromptTurnEditor
# ---------------------------------------------------------------------------


class TestPromptTurnEditor:
    def test_append_builds_separator_correctly(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p2 = _static_part("task", "t", "Task")
        turn = PromptTurnEditor().append(p1).append(p2).build()
        assert turn.text == "Role\n\nTask"
        assert len(turn.segments) == 2
        assert turn.segments[0].text == "Role"
        assert turn.segments[1].text == "\n\nTask"

    def test_prepend_inserts_before_existing(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p2 = _static_part("bootstrap", "b", "Bootstrap")
        turn = PromptTurnEditor().append(p1).prepend(p2).build()
        assert turn.text == "Bootstrap\n\nRole"

    def test_empty_body_part_preserved_in_prepend_with_zero_wire_bytes(self) -> None:
        # ADR 0060: empty-body parts carry envelope/cache identity and must
        # be kept as a text="" segment (zero wire bytes), not dropped.
        p_empty = _static_part("role", "empty", "")
        p_real = _static_part("task", "t", "Task")
        turn = PromptTurnEditor().append(p_real).prepend(p_empty).build()
        assert turn.text == "Task"
        assert len(turn.segments) == 2
        assert turn.segments[0].part is p_empty
        assert turn.segments[0].text == ""
        # the empty part is still visible in parts/envelope identity
        assert p_empty in turn.parts

    def test_empty_body_part_preserved_in_append_with_zero_wire_bytes(self) -> None:
        p_real = _static_part("role", "r", "Role")
        p_empty = _static_part("task", "empty", "")
        turn = PromptTurnEditor().append(p_real).append(p_empty).build()
        assert turn.text == "Role"
        assert len(turn.segments) == 2
        assert turn.segments[1].part is p_empty
        assert turn.segments[1].text == ""
        assert p_empty in turn.parts

    def test_inject_mid_inserts_after_segment(self) -> None:
        p1 = _static_part("role", "r", "A")
        p2 = _static_part("task", "t", "C")
        p_mid = _static_part("format", "f", "B")
        # Now do it properly with inject_mid
        editor2 = PromptTurnEditor()
        editor2.append(p1)
        editor2.append(p2)
        # rebuild to get segment IDs
        t2 = editor2.build()
        editor3 = PromptTurnEditor(t2)
        editor3.inject_mid(p_mid, after_segment_id=t2.segments[0].segment_id)
        final = editor3.build()
        assert final.text == "A\n\nB\n\nC"

    def test_inject_mid_raises_for_unknown_segment_id(self) -> None:
        p1 = _static_part("role", "r", "A")
        p2 = _static_part("task", "t", "B")
        editor = PromptTurnEditor()
        editor.append(p1)
        with pytest.raises(ValueError, match="not found"):
            editor.inject_mid(p2, after_segment_id="nonexistent")

    def test_base_turn_preserved(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p2 = _static_part("task", "t", "Task")
        s1 = PromptSegment(text="Role", part=p1, segment_id="orig:0")
        base = PromptTurn(segments=(s1,))
        turn = PromptTurnEditor(base).append(p2).build()
        assert turn.text == "Role\n\nTask"


# ---------------------------------------------------------------------------
# PromptTurn.render_selected
# ---------------------------------------------------------------------------


class TestRenderSelected:
    def test_full_turn_unchanged(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p2 = _static_part("task", "t", "Task")
        turn = PromptTurnEditor().append(p1).append(p2).build()
        selected = turn.render_selected(list(turn.parts))
        assert selected.text == turn.text

    def test_delta_subset_byte_parity(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p2 = _static_part("task", "t", "Task")
        p3 = _volatile_part("artifact", "a", "Artifact")
        turn = PromptTurnEditor().append(p1).append(p2).append(p3).build()
        # Select only p1 and p3 — skipping p2
        selected = turn.render_selected([p1, p3])
        expected = "Role\n\nArtifact"
        assert selected.text == expected

    def test_empty_selection_returns_empty_turn(self) -> None:
        p1 = _static_part("role", "r", "Role")
        turn = PromptTurnEditor().append(p1).build()
        result = turn.render_selected([])
        assert result.text == ""
        assert result.segments == ()

    def test_selected_part_with_no_matching_segment_fails_fast(self) -> None:
        # A selected part absent from the source turn must raise, not get
        # silently dropped — silent truncation would ship an incomplete delta
        # while cache bookkeeping believed the part was sent.
        p1 = _static_part("role", "r", "Role")
        p_foreign = _static_part("task", "t", "Foreign")
        turn = PromptTurnEditor().append(p1).build()
        with pytest.raises(ValueError, match="no .*matching segment"):
            turn.render_selected([p1, p_foreign])

    def test_over_selecting_repeated_part_fails_fast(self) -> None:
        # Part appears once in the turn; selecting it twice must raise on the
        # second (out-of-range) occurrence.
        p1 = _static_part("role", "r", "Role")
        turn = PromptTurnEditor().append(p1).build()
        with pytest.raises(ValueError, match="no .*matching segment"):
            turn.render_selected([p1, p1])

    def test_rebased_segment_ids_carry_prefix(self) -> None:
        p1 = _static_part("role", "r", "Role")
        turn = PromptTurnEditor().append(p1).build()
        selected = turn.render_selected([p1])
        assert selected.segments[0].segment_id.startswith("rebased:")

    def test_rebased_source_segment_id_links_to_original(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p2 = _static_part("task", "t", "Task")
        p3 = _volatile_part("artifact", "a", "Artifact")
        turn = PromptTurnEditor().append(p1).append(p2).append(p3).build()
        original_ids = [s.segment_id for s in turn.segments]
        # Source-turn segments default source_segment_id to their own segment_id
        for seg in turn.segments:
            assert seg.source_segment_id == seg.segment_id

        # Rebase a delta subset (skip p2)
        selected = turn.render_selected([p1, p3])
        rebased_ids = [s.segment_id for s in selected.segments]
        source_ids = [s.source_segment_id for s in selected.segments]

        # New segment_ids are unique among themselves and disjoint from originals.
        assert len(set(rebased_ids)) == len(rebased_ids)
        assert set(rebased_ids).isdisjoint(set(original_ids))

        # source_segment_id points back at the original segment_ids in order.
        assert source_ids == [original_ids[0], original_ids[2]]
        # And rebased segment_id is the documented "rebased:<source>" form.
        for seg in selected.segments:
            assert seg.segment_id == f"rebased:{seg.source_segment_id}"

    def test_empty_body_rebased_segment_preserves_source_link(self) -> None:
        # PromptTurnEditor drops empty-body parts, so build the source turn
        # manually to exercise the empty-body branch of render_selected.
        p_empty = _static_part("context", "c", "")
        p_role = _static_part("role", "r", "Role")
        s_empty = PromptSegment(text="", part=p_empty, segment_id="orig:empty:0")
        s_role = PromptSegment(text="Role", part=p_role, segment_id="orig:role:1")
        turn = PromptTurn(segments=(s_empty, s_role))
        selected = turn.render_selected([p_empty, p_role])
        assert selected.segments[0].text == ""
        assert selected.segments[0].source_segment_id == "orig:empty:0"
        assert selected.segments[1].source_segment_id == "orig:role:1"


# ---------------------------------------------------------------------------
# assemble_cache_first_segments byte-parity
# ---------------------------------------------------------------------------


class TestAssembleCacheFirstSegments:
    def test_text_byte_parity_with_join_contract(self) -> None:
        parts = [
            _static_part("role", "r", "Role"),
            _static_part("task", "t", "Task"),
            _static_part("format", "f", "Format"),
        ]
        turn = assemble_cache_first_segments(parts)
        # Contract: byte-identical to "\n\n".join(p.body for p in ordered if p.body).strip()
        # (we can't call old assembler, but we can verify the invariant directly)
        non_empty_bodies = [p.body for p in turn.parts if p.body]
        expected = "\n\n".join(non_empty_bodies)
        assert turn.text == expected

    def test_empty_body_parts_contribute_zero_bytes(self) -> None:
        p1 = _static_part("role", "r", "Role")
        p_empty = _static_part("context", "c", "")
        p2 = _static_part("task", "t", "Task")
        turn = assemble_cache_first_segments([p1, p_empty, p2])
        assert turn.text == "Role\n\nTask"

    def test_empty_input_returns_empty_turn(self) -> None:
        turn = assemble_cache_first_segments([])
        assert turn.text == ""
        assert turn.segments == ()

    def test_kind_order_role_before_task(self) -> None:
        task_part = _static_part("task", "t", "Task")
        role_part = _static_part("role", "r", "Role")
        # Pass task before role — assembler must sort role before task
        turn = assemble_cache_first_segments([task_part, role_part])
        assert turn.parts[0].kind == "role"
        assert turn.parts[1].kind == "task"

    def test_system_tail_leads_tier(self) -> None:
        role_part = _static_part("role", "r", "Role")
        tail_part = _static_part("system_tail", "st", "Contract")
        turn = assemble_cache_first_segments([role_part, tail_part])
        assert turn.parts[0].kind == "system_tail"


# ---------------------------------------------------------------------------
# hypothesis_suffix_part — Bug 3 regression
# ---------------------------------------------------------------------------


class TestHypothesisSuffixPart:
    def test_strips_leading_newlines(self) -> None:
        body_with_prefix = "\n\nHypothesis context text"
        part = hypothesis_suffix_part(body_with_prefix)
        assert part.body == "Hypothesis context text"

    def test_no_double_separator_when_appended(self) -> None:
        """Regression: Bug 3 — hypothesis context block must not introduce
        double separator when appended via PromptTurnEditor."""
        base_part = _static_part("role", "r", "Base prompt")
        hyp_part = hypothesis_suffix_part("\n\nHypothesis context")
        turn = PromptTurnEditor().append(base_part).append(hyp_part).build()
        # Should be exactly one \n\n between base and hypothesis
        assert turn.text == "Base prompt\n\nHypothesis context"
        assert "\n\n\n" not in turn.text

    def test_hypothesis_part_in_trace_view(self) -> None:
        """Regression: Bug 3 — hypothesis_suffix must be visible in trace_view."""
        base_part = _static_part("role", "r", "Base")
        hyp_part = hypothesis_suffix_part("Hypothesis block")
        turn = PromptTurnEditor().append(base_part).append(hyp_part).build()
        tv = turn.trace_view()
        part_kinds = [s.part.kind for s in tv.segments]
        assert "hypothesis_suffix" in part_kinds

    def test_hypothesis_part_metadata(self) -> None:
        part = hypothesis_suffix_part("Some text")
        assert part.kind == "hypothesis_suffix"
        assert part.source == "artifact"
        assert part.stability == PromptStability.TURN
        assert part.cache_scope == PromptCacheScope.NONE
        assert part.volatile_reason is not None
