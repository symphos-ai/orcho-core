"""Tests for ``pipeline.control.operator_decisions``."""
from __future__ import annotations

import pytest

from pipeline.control.operator_decisions import (
    OperatorDecisionError,
    parse_operator_decisions,
)


class TestParseOperatorDecisions:
    """The shared ``--decision`` parser is per-subcommand: ``cross``
    accepts ``contract_check=run|skip`` today; ``run`` accepts no
    targets and rejects any supplied target loudly.
    """

    def test_no_decisions_returns_empty(self) -> None:
        assert parse_operator_decisions(None, None, subcommand="cross") == ()
        assert parse_operator_decisions((), None, subcommand="cross") == ()

    def test_single_run_decision_parses(self) -> None:
        out = parse_operator_decisions(
            ["contract_check=run"], None, subcommand="cross",
        )
        assert len(out) == 1
        assert out[0].target == "contract_check"
        assert out[0].decision == "run"
        assert out[0].feedback == ""

    def test_single_skip_decision_parses(self) -> None:
        out = parse_operator_decisions(
            ["contract_check=skip"], None, subcommand="cross",
        )
        assert out[0].decision == "skip"

    def test_feedback_attaches_to_single_decision(self) -> None:
        out = parse_operator_decisions(
            ["contract_check=skip"],
            "Tiny docs-only change.",
            subcommand="cross",
        )
        assert out[0].feedback == "Tiny docs-only change."

    def test_malformed_target_pair_rejected(self) -> None:
        with pytest.raises(OperatorDecisionError, match="TARGET=DECISION"):
            parse_operator_decisions(
                ["contract_check"], None, subcommand="cross",
            )

    def test_unknown_target_rejected(self) -> None:
        with pytest.raises(OperatorDecisionError, match="unknown target"):
            parse_operator_decisions(
                ["unknown_gate=run"], None, subcommand="cross",
            )

    def test_unsupported_decision_rejected(self) -> None:
        with pytest.raises(OperatorDecisionError, match="unsupported decision"):
            parse_operator_decisions(
                ["contract_check=maybe"], None, subcommand="cross",
            )

    def test_duplicate_target_rejected(self) -> None:
        with pytest.raises(OperatorDecisionError, match="duplicate target"):
            parse_operator_decisions(
                ["contract_check=run", "contract_check=skip"],
                None, subcommand="cross",
            )

    def test_feedback_without_decision_rejected(self) -> None:
        with pytest.raises(OperatorDecisionError, match="without --decision"):
            parse_operator_decisions(
                None, "feedback only", subcommand="cross",
            )

    def test_feedback_with_multiple_decisions_rejected(self) -> None:
        # Today the only registered cross target is ``contract_check``;
        # supplying it twice trips the duplicate-target rule, not the
        # ambiguity rule, so we simulate two distinct targets by
        # temporarily extending the per-subcommand allowlist.
        from pipeline.control import operator_decisions as mod

        original = dict(mod.DECISION_TARGETS_BY_SUBCOMMAND["cross"])
        try:
            mod.DECISION_TARGETS_BY_SUBCOMMAND["cross"]["validate_plan"] = (  # type: ignore[index]
                frozenset({"approve", "reject"})
            )
            with pytest.raises(OperatorDecisionError, match="exactly one"):
                parse_operator_decisions(
                    ["contract_check=run", "validate_plan=approve"],
                    "ambiguous",
                    subcommand="cross",
                )
        finally:
            mod.DECISION_TARGETS_BY_SUBCOMMAND["cross"].clear()  # type: ignore[union-attr]
            mod.DECISION_TARGETS_BY_SUBCOMMAND["cross"].update(  # type: ignore[union-attr]
                original,
            )

    def test_run_subcommand_rejects_unknown_target(self) -> None:
        # ``run`` now exposes ``commit`` in the allowlist, so ``contract_check``
        # is rejected as an unknown target (not "not applicable").
        with pytest.raises(OperatorDecisionError, match="unknown target"):
            parse_operator_decisions(
                ["contract_check=run"], None, subcommand="run",
            )

    def test_unknown_subcommand_rejected(self) -> None:
        with pytest.raises(OperatorDecisionError, match="unknown subcommand"):
            parse_operator_decisions(
                ["contract_check=run"], None, subcommand="bogus",
            )

    @pytest.mark.parametrize("verb", ["fix", "approve", "apply", "skip", "halt"])
    def test_run_subcommand_accepts_commit_verbs(self, verb: str) -> None:
        out = parse_operator_decisions(
            [f"commit={verb}"], None, subcommand="run",
        )
        assert len(out) == 1
        assert out[0].target == "commit"
        assert out[0].decision == verb
        assert out[0].feedback == ""

    @pytest.mark.parametrize("verb", ["fix", "approve", "apply", "skip", "halt"])
    def test_cross_subcommand_accepts_commit_verbs(self, verb: str) -> None:
        out = parse_operator_decisions(
            [f"commit={verb}"], None, subcommand="cross",
        )
        assert len(out) == 1
        assert out[0].target == "commit"
        assert out[0].decision == verb

    def test_run_subcommand_rejects_unknown_commit_verb(self) -> None:
        with pytest.raises(OperatorDecisionError, match="unsupported decision"):
            parse_operator_decisions(
                ["commit=foo"], None, subcommand="run",
            )

    def test_cross_subcommand_rejects_unknown_commit_verb(self) -> None:
        with pytest.raises(OperatorDecisionError, match="unsupported decision"):
            parse_operator_decisions(
                ["commit=foo"], None, subcommand="cross",
            )

    def test_commit_with_feedback_on_run(self) -> None:
        out = parse_operator_decisions(
            ["commit=approve"], "note", subcommand="run",
        )
        assert out[0].target == "commit"
        assert out[0].decision == "approve"
        assert out[0].feedback == "note"
