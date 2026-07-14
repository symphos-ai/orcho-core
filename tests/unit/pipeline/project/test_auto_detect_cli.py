"""Stage C auto-detect CLI topology choices (T3) — isolated unit tests.

Exercises :func:`cli._profile_prompt.resolve_topology_choice` — the CLI logic
that, for a high-confidence ``cross_recommended`` resolution, prints the
``Auto-detect result`` block + three explicit choices and maps the operator's
pick to a delivery scope:

* the three choices map to ``CROSS`` / ``EXPANDED_MONO`` / ``STRICT_MONO``;
* choice 1 (start cross) never starts a cross pipeline inside the current mono
  process — it raises ``CrossRunRequested`` so the caller launches a fresh one;
* a non-interactive context shows no block, never starts cross, and never
  widens delivery (``delivery_scope`` stays ``strict_mono``);
* a non-cross / low-confidence resolution is returned unchanged.

No real provider detector and no real ``run_pipeline`` are invoked — the
resolution is built with the deterministic T1/T2 vocabulary and the choice is
injected via ``choice_fn``.
"""

from __future__ import annotations

import pytest

from cli._profile_menu import render_autodetect_result
from cli._profile_prompt import CrossRunRequested, resolve_topology_choice
from pipeline.runtime.run_shape import (
    DeliveryScope,
    OperatingMode,
    RunTopology,
    SemanticProfile,
)
from pipeline.runtime.work_kind_detection import (
    AutoDetectPolicy,
    AutoDetectResolution,
    DetectionState,
)


def _cross_resolution(confidence: float = 0.9) -> AutoDetectResolution:
    """A recommended resolution carrying a high-confidence cross topology."""
    return AutoDetectResolution(
        detection_state=DetectionState.RECOMMENDED,
        actual_profile=SemanticProfile.FEATURE,
        actual_mode=OperatingMode.PRO,
        policy=AutoDetectPolicy.CONFIRM,
        recommended_profile=SemanticProfile.FEATURE,
        recommended_mode=OperatingMode.PRO,
        confidence=confidence,
        rationale="looks like a feature",
        risk_flags=("schema",),
        recommended_topology=RunTopology.CROSS_RECOMMENDED,
        delivery_projects=("orcho-core", "orcho-mcp"),
        topology_reason="core SDK wire change likely requires MCP update",
    )


# ── choice → delivery_scope mapping ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (2, DeliveryScope.EXPANDED_MONO),
        (3, DeliveryScope.STRICT_MONO),
    ],
)
def test_each_mono_choice_maps_to_expected_scope(
    number: int, expected: DeliveryScope, capsys: pytest.CaptureFixture[str],
) -> None:
    # Choices 2 / 3 keep the mono run going and only set its delivery_scope.
    # Choice 1 (start cross) is a terminal directive — covered separately.
    res = _cross_resolution()
    assert res.delivery_scope is DeliveryScope.STRICT_MONO  # before choice
    updated = resolve_topology_choice(
        res, interactive=True, color=False, choice_fn=lambda: number,
    )
    assert updated.delivery_scope is expected
    # The choice never mutates the resolved profile / mode / topology.
    assert updated.actual_profile is res.actual_profile
    assert updated.actual_mode is res.actual_mode
    assert updated.recommended_topology is RunTopology.CROSS_RECOMMENDED
    # The result block was rendered.
    out = capsys.readouterr().out
    assert "Auto-detect result" in out
    assert "cross recommended" in out


def test_block_lists_three_choices_and_projects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = _cross_resolution()
    resolve_topology_choice(
        res, interactive=True, color=False, choice_fn=lambda: 3,
    )
    out = capsys.readouterr().out
    assert "Start cross run with these projects" in out
    assert "Continue mono run and allow expanded delivery" in out
    assert "Continue strict mono" in out
    assert "orcho-core, orcho-mcp" in out


# ── choice 1 raises a launch directive, never mutates the mono run ───────────


def test_choice_cross_raises_launch_directive_not_a_mono_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Choice 1 (start cross) raises a terminal directive instead of returning a
    # cross-scoped resolution: the current mono run must NOT continue and must
    # NOT persist delivery_scope=cross. The caller launches a fresh cross run
    # from ``.projects``; no copy-paste template is printed here anymore.
    res = _cross_resolution()
    with pytest.raises(CrossRunRequested) as excinfo:
        resolve_topology_choice(
            res, interactive=True, color=False, choice_fn=lambda: 1,
        )
    assert excinfo.value.projects == ("orcho-core", "orcho-mcp")
    out = capsys.readouterr().out
    # The result block still renders, but the old manual-launch template is gone.
    assert "Auto-detect result" in out
    assert "To start the cross run" not in out
    assert "<path>" not in out


# ── non-interactive: record only, no cross, no widening ──────────────────────


def test_non_interactive_surfaces_recommendation_without_widening(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = _cross_resolution()

    def _must_not_prompt() -> int:
        raise AssertionError("choice_fn must not run in a non-interactive run")

    updated = resolve_topology_choice(
        res, interactive=False, color=False, choice_fn=_must_not_prompt,
    )
    # No widening, no cross start — delivery stays strict_mono.
    assert updated.delivery_scope is DeliveryScope.STRICT_MONO
    assert updated.recommended_topology is RunTopology.CROSS_RECOMMENDED
    # Cross parity: the recommendation is SURFACED (not silently swallowed) —
    # the headless block echoes the topology + a ready `orcho cross` directive,
    # but without the interactive 1/2/3 choices.
    out = capsys.readouterr().out
    assert "Auto-detect result" in out
    assert "orcho cross --projects" in out
    assert "mono" in out
    assert "Choices" not in out


# ── pass-through cases ───────────────────────────────────────────────────────


def test_mono_recommendation_is_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = AutoDetectResolution(
        detection_state=DetectionState.RECOMMENDED,
        actual_profile=SemanticProfile.SMALL_TASK,
        actual_mode=OperatingMode.FAST,
        policy=AutoDetectPolicy.CONFIRM,
        recommended_profile=SemanticProfile.SMALL_TASK,
        recommended_mode=OperatingMode.FAST,
        confidence=0.95,
    )
    updated = resolve_topology_choice(
        res, interactive=True, color=False,
        choice_fn=lambda: pytest.fail("must not prompt for a mono run"),
    )
    assert updated is res
    assert updated.delivery_scope is DeliveryScope.STRICT_MONO
    assert capsys.readouterr().out == ""


def test_low_confidence_cross_is_not_offered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A cross topology echo below the confidence floor is recorded but never
    # surfaced as a delivery-widening prompt.
    res = _cross_resolution(confidence=0.4)
    updated = resolve_topology_choice(
        res, interactive=True, color=False,
        choice_fn=lambda: pytest.fail("must not prompt below the floor"),
    )
    assert updated is res
    assert updated.delivery_scope is DeliveryScope.STRICT_MONO
    assert "Auto-detect result" not in capsys.readouterr().out


# ── pure rendering helpers ───────────────────────────────────────────────────


def test_render_autodetect_result_is_provider_neutral(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_autodetect_result(_cross_resolution(), color=False)
    out = capsys.readouterr().out
    assert "profile" in out and "feature" in out
    assert "cross recommended" in out
    assert "confidence" in out
    assert "projects" in out
