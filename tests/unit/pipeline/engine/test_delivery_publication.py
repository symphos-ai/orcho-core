"""Unit coverage for folding a publish result into durable delivery facts."""
from __future__ import annotations

from pipeline.engine.delivery_branch import DeliveryBranchOutcome
from pipeline.engine.delivery_publication import publication_facts
from pipeline.engine.delivery_publish import PublishResult


def _outcome(**overrides: object) -> DeliveryBranchOutcome:
    fields: dict[str, object] = {
        "policy": "worktree_branch",
        "plan": "publish",
        "default_branch": "main",
        "base_ref": "main",
        "delivery_branch": "orcho/deliver/r1-x",
        "notices": ("prior notice",),
    }
    fields.update(overrides)
    return DeliveryBranchOutcome(**fields)  # type: ignore[arg-type]


def test_missing_pr_keeps_branch_ready_notice() -> None:
    facts = publication_facts(_outcome(), PublishResult(pushed=True))

    assert facts.pr_url is None
    assert any("is ready" in notice for notice in facts.delivery_notices)


def test_bypass_outcome_without_branch_keeps_notices_unchanged() -> None:
    outcome = _outcome(
        policy="bypass", plan="commit_in_place", delivery_branch=None,
    )

    facts = publication_facts(outcome, PublishResult(pushed=False))

    assert facts.delivery_branch is None
    assert facts.pr_url is None
    assert facts.delivery_notices == ("prior notice",)
