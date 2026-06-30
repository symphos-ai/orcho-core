"""Stage C auto-detect topology + delivery-scope threading (T2).

Asserts the three axes — semantic profile, recommended topology, and delivery
scope — flow through :func:`resolve_auto_detect`, the durable payload, and the
explicit operator-choice model without the cross *recommendation* ever
silently converting a mono run into a cross run or widening delivery:

* a signal-bearing decision resolves with ``recommended_topology ==
  cross_recommended`` and ``delivery_projects`` containing ``orcho-core`` +
  ``orcho-mcp``, while ``actual_profile`` is the recommended profile and
  ``delivery_scope`` stays ``strict_mono``;
* a concrete ``--profile`` never reaches the dispatcher (the auto-detect token
  is not a profile), and trusted / non-interactive resolution keeps
  ``delivery_scope == strict_mono`` under a cross recommendation;
* ``apply_topology_choice`` maps the three explicit operator choices to
  ``CROSS`` / ``EXPANDED_MONO`` / ``STRICT_MONO`` without mutating
  ``actual_profile`` / ``recommended_topology``;
* ``resolution_to_payload`` round-trips every new field through
  ``AutoDetectResolution`` (the run_setup persistence path), and the scoped env
  channel does not leak the new fields into a later manual run.

The provider-backed detector is never invoked (autouse guard); deterministic
T1 fakes carry the topology echo.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest

from pipeline.project.auto_detect import (
    AUTO_DETECT_PROFILE_TOKEN,
    AUTODETECT_DECISION_ENV,
    ProviderWorkKindDetector,
    TopologyChoice,
    apply_topology_choice,
    resolution_to_payload,
    resolve_auto_detect,
    scoped_autodetect_decision_env,
)
from pipeline.runtime.run_shape import (
    DeliveryScope,
    RunTopology,
    SemanticProfile,
)
from pipeline.runtime.topology_detection import recommend_topology
from pipeline.runtime.work_kind_detection import (
    AutoDetectConfig,
    AutoDetectDecision,
    AutoDetectResolution,
    DetectionState,
    RaisingWorkKindDetector,
    StaticWorkKindDetector,
)

# A self-contained signal table so the helper does not depend on config I/O.
_SIGNALS = {
    "mcp schema": ["orcho-core", "orcho-mcp"],
    "sdk wire": ["orcho-core", "orcho-mcp"],
}


@pytest.fixture(autouse=True)
def _forbid_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No unit test may invoke the provider-backed detector."""
    def _boom(self, *, task: str, project: str):  # noqa: ANN001
        raise AssertionError(
            "ProviderWorkKindDetector.detect must never run in unit tests"
        )
    monkeypatch.setattr(ProviderWorkKindDetector, "detect", _boom)


@pytest.fixture(autouse=True)
def _clean_decision_env() -> Iterator[None]:
    before = os.environ.get(AUTODETECT_DECISION_ENV)
    os.environ.pop(AUTODETECT_DECISION_ENV, None)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(AUTODETECT_DECISION_ENV, None)
        else:
            os.environ[AUTODETECT_DECISION_ENV] = before


def _config(**overrides) -> AutoDetectConfig:
    base = {
        "policy": "trust_above_threshold",
        "confidence_threshold": 0.7,
        "fallback_profile": "feature",
        "on_low_confidence": "fallback",
        "on_error": "fallback",
    }
    base.update(overrides)
    return AutoDetectConfig.parse(base)


def _cross_decision(
    profile: str = "feature", mode: str = "pro", confidence: float = 0.9
) -> AutoDetectDecision:
    """A decision whose topology echo mirrors what detect() merges in.

    The semantic profile + mode stand in for the model's pick; the topology
    fields come from the deterministic heuristic over a signal-bearing task.
    """
    topo = recommend_topology(
        "change the sdk wire and regenerate the mcp schema", signals=_SIGNALS
    )
    assert topo.topology is RunTopology.CROSS_RECOMMENDED  # guard the fixture
    return AutoDetectDecision(
        recommended_profile=profile,
        recommended_mode=mode,
        confidence=confidence,
        rationale="because reasons",
        risk_flags=("schema",),
        recommended_topology=topo.topology,
        delivery_projects=topo.projects,
        topology_reason=topo.reason,
    )


# ── (a) signal task → cross_recommended + projects, scope stays strict ───────


def test_signal_task_resolution_carries_cross_topology() -> None:
    det = StaticWorkKindDetector(_cross_decision())
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.recommended_topology is RunTopology.CROSS_RECOMMENDED
    assert "orcho-core" in res.delivery_projects
    assert "orcho-mcp" in res.delivery_projects
    assert res.delivery_projects[0] == "orcho-core"
    assert res.topology_reason != ""


def test_detector_error_resolution_keeps_mono_defaults() -> None:
    # No decision exists → topology echo stays at the inert defaults.
    det = RaisingWorkKindDetector(RuntimeError("boom"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(on_error="fallback"), detector=det,
    )
    assert res.detection_state is DetectionState.DETECTOR_ERROR_FALLBACK
    assert res.recommended_topology is RunTopology.MONO
    assert res.delivery_projects == ()
    assert res.topology_reason == ""
    assert res.delivery_scope is DeliveryScope.STRICT_MONO


# ── (b) explicit profile bypass + trust keeps strict_mono under cross ────────


def test_explicit_concrete_profile_never_enters_resolve() -> None:
    # The CLI gate calls resolve_auto_detect ONLY for the auto-detect token.
    # A concrete --profile is a valid SemanticProfile, so it never matches the
    # token and never becomes a cross run.
    assert SemanticProfile("feature") is SemanticProfile.FEATURE
    with pytest.raises(ValueError):
        SemanticProfile(AUTO_DETECT_PROFILE_TOKEN)


def test_trust_non_interactive_cross_keeps_strict_mono_and_profile() -> None:
    # KEY INVARIANT: a cross recommendation under trusted / non-interactive
    # resolution does NOT change actual_profile and does NOT widen delivery.
    det = StaticWorkKindDetector(_cross_decision(profile="feature"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(
            policy="trust_above_threshold", confidence_threshold=0.7,
        ),
        detector=det,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.recommended_topology is RunTopology.CROSS_RECOMMENDED
    # actual_profile is the recommended profile — not a cross conversion.
    assert res.actual_profile is SemanticProfile.FEATURE
    # delivery_scope stays strict_mono regardless of the cross recommendation.
    assert res.delivery_scope is DeliveryScope.STRICT_MONO


def test_apply_topology_choice_maps_three_variants_to_scope() -> None:
    det = StaticWorkKindDetector(_cross_decision())
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    assert res.delivery_scope is DeliveryScope.STRICT_MONO

    cross = apply_topology_choice(res, TopologyChoice.START_CROSS)
    assert cross.delivery_scope is DeliveryScope.CROSS
    # An explicit choice never silently mutates the run's profile / topology.
    assert cross.actual_profile is res.actual_profile
    assert cross.recommended_topology is res.recommended_topology

    expanded = apply_topology_choice(res, TopologyChoice.EXPANDED_MONO)
    assert expanded.delivery_scope is DeliveryScope.EXPANDED_MONO

    strict = apply_topology_choice(res, TopologyChoice.STRICT_MONO)
    assert strict.delivery_scope is DeliveryScope.STRICT_MONO

    # The 1-based operator choice numbers map onto the typed members.
    assert TopologyChoice.from_number(1) is TopologyChoice.START_CROSS
    assert TopologyChoice.from_number(2) is TopologyChoice.EXPANDED_MONO
    assert TopologyChoice.from_number(3) is TopologyChoice.STRICT_MONO
    with pytest.raises(ValueError):
        TopologyChoice.from_number(4)


# ── (c) payload round-trip + scoped env isolation ────────────────────────────


def test_payload_roundtrips_with_topology_fields() -> None:
    det = StaticWorkKindDetector(_cross_decision())
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    res = apply_topology_choice(res, TopologyChoice.EXPANDED_MONO)

    payload = resolution_to_payload(res)
    assert payload["recommended_topology"] == "cross_recommended"
    assert payload["delivery_projects"] == ["orcho-core", "orcho-mcp"]
    assert payload["delivery_scope"] == "expanded_mono"
    assert payload["topology_reason"]

    # Round-trips through AutoDetectResolution — the run_setup validation path.
    rebuilt = AutoDetectResolution(**payload)
    assert rebuilt.recommended_topology is RunTopology.CROSS_RECOMMENDED
    assert rebuilt.delivery_projects == ("orcho-core", "orcho-mcp")
    assert rebuilt.delivery_scope is DeliveryScope.EXPANDED_MONO
    assert resolution_to_payload(rebuilt) == payload


def test_scoped_env_carries_topology_then_does_not_leak() -> None:
    det = StaticWorkKindDetector(_cross_decision())
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    with scoped_autodetect_decision_env(res):
        payload = json.loads(os.environ[AUTODETECT_DECISION_ENV])
        assert payload["recommended_topology"] == "cross_recommended"
        assert payload["delivery_scope"] == "strict_mono"
        assert payload["delivery_projects"] == ["orcho-core", "orcho-mcp"]
    # Channel cleared on exit — no new field leaks past the run.
    assert AUTODETECT_DECISION_ENV not in os.environ
    # A subsequent manual run (resolution None) sees nothing.
    with scoped_autodetect_decision_env(None):
        assert AUTODETECT_DECISION_ENV not in os.environ
    assert AUTODETECT_DECISION_ENV not in os.environ
