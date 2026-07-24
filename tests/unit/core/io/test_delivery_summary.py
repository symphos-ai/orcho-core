"""Unit coverage for the durable publish-outcome projection."""
from __future__ import annotations

from core.io.delivery_summary import project_degraded_publish


def _delivery(**overrides: object) -> dict[str, object]:
    delivery: dict[str, object] = {
        "delivery_branch": "orcho/deliver/r1-feature",
        "pr_url": None,
        "delivery_warnings": (),
        "delivery_notices": (),
    }
    delivery.update(overrides)
    return delivery


def test_successful_publish_and_off_gate_are_not_degraded() -> None:
    assert project_degraded_publish(
        _delivery(pr_url="https://example.test/pr/7"), publish_gate="always",
    ) is None
    assert project_degraded_publish(
        _delivery(delivery_notices=(
            "delivery branch orcho/deliver/r1-feature is ready; "
            "open a pull request",
        )),
        publish_gate="off",
    ) is None


def test_auto_local_delivery_without_publish_signal_is_not_degraded() -> None:
    assert project_degraded_publish(_delivery(), publish_gate="auto") is None


def test_degraded_publish_uses_one_publish_warning_and_safe_reason() -> None:
    outcome = project_degraded_publish(
        _delivery(
            delivery_warnings=(
                "ordinary local diagnostic",
                "\x1b[31mdelivery publish provider failed\x1b[0m\nbecause auth expired",
                "delivery push retry was skipped",
            ),
            delivery_notices=(
                "delivery branch orcho/deliver/r1-feature is ready; open a pull request",
            ),
        ),
        publish_gate="always",
    )

    assert outcome is not None
    assert outcome.ready_text == "branch orcho/deliver/r1-feature ready"
    assert outcome.reason == "delivery publish provider failed because auth expired"


def test_ready_notice_without_warning_uses_honest_fallback() -> None:
    outcome = project_degraded_publish(
        _delivery(delivery_notices=(
            "delivery branch orcho/deliver/r1-feature is ready; open a pull request",
        )),
        publish_gate="auto",
    )

    assert outcome is not None
    assert outcome.reason == "publication did not return a PR URL"
