"""Durable facts derived from a provider-neutral delivery publication.

This module deliberately contains no provider discovery or invocation.  The
single provider seam remains :func:`pipeline.engine.delivery_publish.publish_delivery`;
the commit site uses these facts to persist a provider result consistently for
both local delivery-branch plans.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline.engine.delivery_branch import DeliveryBranchOutcome, DeliveryPrIntent
from pipeline.engine.delivery_publish import PublishResult


@dataclass(frozen=True, slots=True)
class DeliveryPublicationFacts:
    """Publish facts ready to fold into a durable delivery decision."""

    delivery_branch: str | None
    pr_intent: DeliveryPrIntent | None
    pr_url: str | None
    delivery_warnings: tuple[str, ...]
    delivery_notices: tuple[str, ...]


def publication_facts(
    outcome: DeliveryBranchOutcome,
    result: PublishResult,
) -> DeliveryPublicationFacts:
    """Fold one provider result into the branch outcome's durable facts.

    A missing PR is intentionally not treated as a failed commit or a proof of
    push.  The resulting notice says only that the already-created branch is
    ready, while provider diagnostics remain visible as non-fatal warnings.
    """
    if result.pr_url:
        notices = outcome.notices + (f"PR opened: {result.pr_url}",)
    elif outcome.delivery_branch:
        notices = outcome.notices + (
            f"delivery branch {outcome.delivery_branch} is ready; "
            "open a pull request or push it manually",
        )
    else:
        notices = outcome.notices
    return DeliveryPublicationFacts(
        delivery_branch=outcome.delivery_branch,
        pr_intent=outcome.pr_intent,
        pr_url=result.pr_url,
        delivery_warnings=outcome.warnings + result.warnings,
        delivery_notices=notices,
    )
