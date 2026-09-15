"""The typed ``review_context`` prompt part (prior-review evidence).

The closing gate may be handed the verdicts of the review attempts that ran
before it. That body is evidence about what a reviewer reported — not a live
blocker list and not proof that anything was verified — so the framing that
says so is code-owned and rides with the body in one typed TURN part.

These tests pin the part's identity and placement, the fact that an absent
context leaves the wire prompt byte-identical, and that the kwarg survives
the ``adapters.run_review`` hop without touching the dry-run path.
"""

from __future__ import annotations

import pytest

from pipeline.plugins import PluginConfig
from pipeline.prompts.builders import runtime_review_uncommitted_prompt
from pipeline.prompts.contracts import review_context_evidence_text
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptStability,
)

_CONTEXT = (
    "Latest review — round 2, pass reverify — APPROVED\n"
    "  superseded: round 2, pass review — REJECTED (F1 blocker: null deref)"
)


def _turn(**kwargs):
    return runtime_review_uncommitted_prompt(
        "check the change",
        project_dir="/proj",
        output_contract="release",
        **kwargs,
    )


def _parts_by_kind(turn) -> dict:
    return {p.kind: p for p in turn.parts}


# ── Empty context is invisible ───────────────────────────────────────────────


class TestEmptyContextChangesNothing:
    @pytest.mark.parametrize(
        "empty", [pytest.param("", id="blank"),
                  pytest.param("   \n  ", id="whitespace")],
    )
    def test_wire_prompt_is_byte_identical(self, empty: str) -> None:
        """A run with no prior review must render exactly what it rendered
        before the part existed."""
        baseline = _turn()
        assert _turn(review_context=empty).text == baseline.text

    def test_no_part_is_emitted(self) -> None:
        assert "review_context" not in _parts_by_kind(_turn(review_context=""))

    def test_framing_is_absent_from_the_wire(self) -> None:
        assert "PRIOR REVIEW EVIDENCE" not in _turn().text


# ── Part identity + placement ────────────────────────────────────────────────


class TestReviewContextPart:
    def test_part_is_present_with_the_pinned_identity(self) -> None:
        part = _parts_by_kind(_turn(review_context=_CONTEXT))["review_context"]
        assert part.id == "review_context:final_acceptance"
        assert part.name == "final_acceptance"
        assert part.source == "artifact"
        assert part.layer is PromptLayer.TURN
        assert part.stability is PromptStability.TURN
        assert part.cache_scope is PromptCacheScope.NONE

    def test_body_carries_the_code_owned_framing_then_the_context(
        self,
    ) -> None:
        part = _parts_by_kind(_turn(review_context=_CONTEXT))["review_context"]
        framing = review_context_evidence_text()
        assert part.body.startswith(framing.split("\n")[0])
        assert part.body.endswith(_CONTEXT)
        assert _CONTEXT in part.body

    def test_framing_states_evidence_not_blocker_and_not_proof(self) -> None:
        """The three claims the framing exists to make: provenance-labelled
        evidence, superseded/invalid ≠ current blocker, nothing here replaces
        the readiness summary."""
        text = review_context_evidence_text()
        assert "PRIOR REVIEW EVIDENCE" in text
        assert "not as a current blocker list" in text
        assert "superseded" in text
        assert "invalid" in text
        assert "readiness summary" in text

    def test_framing_does_not_restate_backstop_or_gap_rules(self) -> None:
        """Engine backstops are the engine's business; restating them here
        would invite the gate to reason about them from prompt text."""
        lowered = review_context_evidence_text().lower()
        for leak in ("backstop", "gap", "receipt", "criterion", "reject"):
            assert leak not in lowered

    def test_body_language_directive_is_appended(self) -> None:
        text = review_context_evidence_text(body_language="Russian")
        assert text.endswith("Write the human-readable JSON fields in Russian.")
        assert "Russian" not in review_context_evidence_text()

    def test_lands_between_readiness_and_current_subject(self) -> None:
        """Readiness stays the leading proof surface; the review evidence
        reads as subordinate to it and ahead of the fresh subject."""
        turn = _turn(
            verification_readiness="Readiness: 1 required receipt missing.",
            review_context=_CONTEXT,
            current_review_subject="diff --git a/x b/x",
        )
        kinds = [p.kind for p in turn.parts]
        assert kinds.index("verification_readiness") < kinds.index(
            "review_context",
        )
        assert kinds.index("review_context") < kinds.index(
            "current_review_subject",
        )

    def test_context_text_reaches_the_wire(self) -> None:
        text = _turn(review_context=_CONTEXT).text
        assert "PRIOR REVIEW EVIDENCE" in text
        assert "pass reverify" in text

    def test_other_review_parts_are_unaffected(self) -> None:
        """Adding the part must not displace the sibling re-review parts."""
        kwargs = dict(
            repair_receipt="Fixed F1.",
            verification_receipt="python 3.12 in /checkout",
            verification_readiness="Readiness: all required receipts present.",
            current_review_subject="diff --git a/x b/x",
        )
        before = _parts_by_kind(_turn(**kwargs))
        after = _parts_by_kind(_turn(review_context=_CONTEXT, **kwargs))
        for kind in before:
            assert after[kind].body == before[kind].body


# ── adapters.run_review proxying ─────────────────────────────────────────────


class TestRunReviewProxy:
    def test_review_context_reaches_the_builder(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.phases import adapters

        captured: dict = {}

        def _fake_invoke(agent, turn, cwd, **kwargs):
            captured["turn"] = turn
            return '{"verdict": "APPROVED", "findings": []}'

        monkeypatch.setattr(adapters, "_invoke_turn", _fake_invoke)
        result = adapters.run_review(
            object(), "[final_acceptance] t", "/checkout", PluginConfig(),
            label="final_acceptance",
            output_contract="release",
            review_context=_CONTEXT,
        )
        assert result.name == "final_acceptance"
        kinds = _parts_by_kind(captured["turn"])
        assert "review_context" in kinds
        assert _CONTEXT in kinds["review_context"].body
        assert "PRIOR REVIEW EVIDENCE" in captured["turn"].text

    def test_default_omits_the_part(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.phases import adapters

        captured: dict = {}

        def _fake_invoke(agent, turn, cwd, **kwargs):
            captured["turn"] = turn
            return '{"verdict": "APPROVED", "findings": []}'

        monkeypatch.setattr(adapters, "_invoke_turn", _fake_invoke)
        adapters.run_review(
            object(), "[final_acceptance] t", "/checkout", PluginConfig(),
            label="final_acceptance",
            output_contract="release",
        )
        assert "review_context" not in _parts_by_kind(captured["turn"])

    def test_dry_run_never_renders_the_context(self) -> None:
        from pipeline.phases import adapters

        result = adapters.run_review(
            object(), "t", "/checkout", PluginConfig(),
            dry_run=True,
            label="final_acceptance",
            output_contract="release",
            review_context="should never land anywhere",
        )
        assert result.meta.get("dry_run") is True
        assert "should never land" not in result.output
        assert "PRIOR REVIEW EVIDENCE" not in result.output
