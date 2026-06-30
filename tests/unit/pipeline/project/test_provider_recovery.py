# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the provider-neutral recovery projection (ADR 0101 / T1).

These pin the pure builder in ``pipeline/project/provider_recovery.py``:
candidates come only from configured ``(runtime, model)`` pairs, the failed
pair is excluded, ``retry``/``halt`` are always present, ``replace`` options
appear only when alternatives exist, and the projection stays provider-neutral.
"""

from __future__ import annotations

from pipeline.project import provider_recovery as pr


def test_configured_pairs_dedup_and_skip_empty_runtime() -> None:
    runtime_map = {
        "plan": "claude",
        "validate_plan": "claude",   # duplicate (claude, sonnet) dropped
        "implement": "codex",
        "review_changes": "",        # no runtime → skipped
    }
    model_map = {
        "plan": "sonnet",
        "validate_plan": "sonnet",
        "implement": "gpt5",
        "review_changes": "anything",
    }
    pairs = pr.configured_runtime_model_pairs(runtime_map, model_map)
    assert pairs == [("claude", "sonnet"), ("codex", "gpt5")]


def test_candidates_exclude_failed_pair() -> None:
    runtime_map = {"plan": "claude", "implement": "codex"}
    model_map = {"plan": "sonnet", "implement": "gpt5"}
    candidates = pr.replacement_candidates(
        failed_runtime="claude",
        failed_model="sonnet",
        runtime_map=runtime_map,
        model_map=model_map,
    )
    assert candidates == [{"runtime": "codex", "model": "gpt5"}]


def test_candidates_only_from_configured_pairs_not_registry() -> None:
    """known_runtimes validates existence only; it is never the pair source."""
    runtime_map = {"plan": "claude", "implement": "codex"}
    model_map = {"plan": "sonnet", "implement": "gpt5"}
    # ``gemini`` is a registered runtime but has no configured pair, so it must
    # NOT appear as a candidate.
    candidates = pr.replacement_candidates(
        failed_runtime="claude",
        failed_model="sonnet",
        runtime_map=runtime_map,
        model_map=model_map,
        known_runtimes=["claude", "codex", "gemini"],
    )
    assert candidates == [{"runtime": "codex", "model": "gpt5"}]


def test_candidate_pruned_when_runtime_not_registered() -> None:
    runtime_map = {"plan": "claude", "implement": "codex"}
    model_map = {"plan": "sonnet", "implement": "gpt5"}
    candidates = pr.replacement_candidates(
        failed_runtime="claude",
        failed_model="sonnet",
        runtime_map=runtime_map,
        model_map=model_map,
        known_runtimes=["claude"],  # codex not registered → pruned
    )
    assert candidates == []


def test_recovery_actions_always_have_retry_and_halt() -> None:
    actions = pr.build_recovery_actions([])
    assert actions == [{"action": "retry"}, {"action": "halt"}]


def test_recovery_actions_append_replace_per_candidate() -> None:
    actions = pr.build_recovery_actions(
        [{"runtime": "codex", "model": "gpt5"}],
    )
    assert actions == [
        {"action": "retry"},
        {"action": "halt"},
        {"action": "replace", "runtime": "codex", "model": "gpt5"},
    ]


def test_build_projection_with_alternatives() -> None:
    projection = pr.build_provider_access_recovery(
        failed_phase="plan",
        runtime="claude",
        model="sonnet",
        runtime_map={"plan": "claude", "implement": "codex"},
        model_map={"plan": "sonnet", "implement": "gpt5"},
    )
    assert projection["failure_kind"] == "provider_access"
    assert projection["recoverable"] is False
    assert projection["recommended_action"] == "switch_runtime_or_restore_access"
    assert projection["failed_phase"] == "plan"
    assert projection["runtime"] == "claude"
    assert projection["model"] == "sonnet"
    assert projection["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
        {"action": "replace", "runtime": "codex", "model": "gpt5"},
    ]


def test_build_projection_without_alternatives_is_retry_halt_only() -> None:
    """A single configured pair (the failed one) yields only retry/halt."""
    projection = pr.build_provider_access_recovery(
        failed_phase="plan",
        runtime="claude",
        model="sonnet",
        runtime_map={"plan": "claude", "validate_plan": "claude"},
        model_map={"plan": "sonnet", "validate_plan": "sonnet"},
    )
    assert projection["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
    ]


def test_projection_is_provider_neutral() -> None:
    """No provider name is hard-coded into the static projection scaffold."""
    projection = pr.build_provider_access_recovery(
        failed_phase="plan",
        runtime="claude",
        model="sonnet",
        runtime_map={"plan": "claude"},
        model_map={"plan": "sonnet"},
    )
    # The neutral scaffold (kind/action/recovery verbs) carries no provider id.
    assert "claude" not in projection["recommended_action"]
    assert all(
        a["action"] in {"retry", "halt", "replace"}
        for a in projection["recovery_actions"]
    )
