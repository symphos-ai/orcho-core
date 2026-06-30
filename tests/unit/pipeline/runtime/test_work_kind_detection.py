"""Stage C auto-detect selector vocabulary — isolated unit tests.

Covers the detector output (``AutoDetectDecision``) confidence validation
and enum closure, the durable resolution invariant
(``AutoDetectResolution`` permits ``recommended_*`` ``None`` only for
detector-error / failed states), the validated configuration
(``AutoDetectConfig`` rejects bad threshold / policy / fallback profile and
loads from ``AppConfig`` over ``config.defaults.json``), and the
deterministic detector fakes (no network / LLM).

These tests do not duplicate the Stage B ``run_shape`` enum tests; they only
assert the auto-detect selector layer and that auto-detect remains a selector,
never an enum member.
"""

from __future__ import annotations

import pytest

from pipeline.runtime.run_shape import OperatingMode, SemanticProfile
from pipeline.runtime.work_kind_detection import (
    AutoDetectConfig,
    AutoDetectDecision,
    AutoDetectPolicy,
    AutoDetectResolution,
    DetectionState,
    FallbackAction,
    RaisingWorkKindDetector,
    StaticWorkKindDetector,
    WorkKindDetector,
)

# ── AutoDetectDecision: confidence validation + enum coercion ────────────────


def test_decision_coerces_enum_strings() -> None:
    d = AutoDetectDecision(
        recommended_profile="feature",
        recommended_mode="fast",
        confidence=0.9,
        rationale="looks like a feature",
        risk_flags=["touches_schema"],
    )
    assert d.recommended_profile is SemanticProfile.FEATURE
    assert d.recommended_mode is OperatingMode.FAST
    assert d.risk_flags == ("touches_schema",)


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0, -1.0])
def test_decision_rejects_out_of_range_confidence(bad: float) -> None:
    with pytest.raises(ValueError):
        AutoDetectDecision(
            recommended_profile=SemanticProfile.FEATURE,
            recommended_mode=OperatingMode.FAST,
            confidence=bad,
        )


@pytest.mark.parametrize("edge", [0.0, 1.0, 0.5])
def test_decision_accepts_in_range_confidence(edge: float) -> None:
    d = AutoDetectDecision(
        recommended_profile=SemanticProfile.FEATURE,
        recommended_mode=OperatingMode.FAST,
        confidence=edge,
    )
    assert d.confidence == edge


def test_decision_rejects_bool_confidence() -> None:
    with pytest.raises(TypeError):
        AutoDetectDecision(
            recommended_profile=SemanticProfile.FEATURE,
            recommended_mode=OperatingMode.FAST,
            confidence=True,  # bool is not a valid confidence
        )


def test_decision_rejects_bad_profile_value() -> None:
    with pytest.raises(ValueError):
        AutoDetectDecision(
            recommended_profile="auto-detect",  # not an enum member
            recommended_mode=OperatingMode.FAST,
            confidence=0.5,
        )


def test_decision_rejects_string_risk_flags() -> None:
    # A bare string must not be silently iterated into characters.
    with pytest.raises(TypeError):
        AutoDetectDecision(
            recommended_profile=SemanticProfile.FEATURE,
            recommended_mode=OperatingMode.FAST,
            confidence=0.5,
            risk_flags="touches_schema",
        )


# ── Enums stay closed; auto-detect is never a profile/mode member ───────────


def test_auto_detect_is_not_a_semantic_profile() -> None:
    with pytest.raises(ValueError):
        SemanticProfile("auto-detect")


def test_auto_detect_is_not_an_operating_mode() -> None:
    with pytest.raises(ValueError):
        OperatingMode("auto-detect")


def test_detection_state_value_set() -> None:
    assert {s.value for s in DetectionState} == {
        "recommended",
        "low_confidence_fallback",
        "detector_error_fallback",
        "failed",
    }


def test_detection_state_rejects_unknown_member() -> None:
    with pytest.raises(ValueError):
        DetectionState("maybe")


# ── AutoDetectResolution invariant ──────────────────────────────────────────


def _recommended_resolution(**overrides: object) -> AutoDetectResolution:
    base = dict(
        detection_state=DetectionState.RECOMMENDED,
        actual_profile=SemanticProfile.FEATURE,
        actual_mode=OperatingMode.FAST,
        policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
        recommended_profile=SemanticProfile.FEATURE,
        recommended_mode=OperatingMode.FAST,
        confidence=0.9,
        rationale="ok",
        confirmation_state="auto",
    )
    base.update(overrides)
    return AutoDetectResolution(**base)  # type: ignore[arg-type]


def test_resolution_recommended_round_trips() -> None:
    res = _recommended_resolution()
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.actual_profile is SemanticProfile.FEATURE
    assert res.actual_mode is OperatingMode.FAST
    assert res.recommended_profile is SemanticProfile.FEATURE


def test_resolution_recommended_requires_recommendation() -> None:
    # RECOMMENDED with a missing recommendation violates the invariant.
    with pytest.raises(ValueError):
        _recommended_resolution(recommended_profile=None)
    with pytest.raises(ValueError):
        _recommended_resolution(recommended_mode=None)


def test_resolution_low_confidence_requires_recommendation() -> None:
    # LOW_CONFIDENCE_FALLBACK still has a decision; recommendation required.
    with pytest.raises(ValueError):
        AutoDetectResolution(
            detection_state=DetectionState.LOW_CONFIDENCE_FALLBACK,
            actual_profile=SemanticProfile.FEATURE,
            actual_mode=OperatingMode.FAST,
            policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
            recommended_profile=None,
            recommended_mode=None,
            fallback_used=True,
            fallback_reason="confidence below threshold",
        )


def test_resolution_low_confidence_fallback_with_recommendation_ok() -> None:
    res = AutoDetectResolution(
        detection_state=DetectionState.LOW_CONFIDENCE_FALLBACK,
        actual_profile=SemanticProfile.FEATURE,
        actual_mode=OperatingMode.FAST,
        policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
        recommended_profile=SemanticProfile.MIGRATION,
        recommended_mode=OperatingMode.GOVERNED,
        confidence=0.3,
        fallback_used=True,
        fallback_reason="confidence below threshold",
    )
    assert res.fallback_used is True
    assert res.recommended_profile is SemanticProfile.MIGRATION


def test_resolution_detector_error_allows_missing_recommendation() -> None:
    res = AutoDetectResolution(
        detection_state=DetectionState.DETECTOR_ERROR_FALLBACK,
        actual_profile=SemanticProfile.FEATURE,
        actual_mode=OperatingMode.FAST,
        policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
        fallback_used=True,
        error_reason="RuntimeError: boom",
        fallback_reason="detector error",
    )
    assert res.recommended_profile is None
    assert res.recommended_mode is None
    assert res.confidence is None
    assert res.error_reason == "RuntimeError: boom"


def test_resolution_failed_allows_missing_recommendation() -> None:
    res = AutoDetectResolution(
        detection_state=DetectionState.FAILED,
        actual_profile=SemanticProfile.FEATURE,
        actual_mode=OperatingMode.FAST,
        policy=AutoDetectPolicy.CONFIRM,
        error_reason="detector error and on_error=fail",
    )
    assert res.recommended_profile is None


def test_resolution_actual_fields_required() -> None:
    with pytest.raises(ValueError):
        AutoDetectResolution(
            detection_state=DetectionState.DETECTOR_ERROR_FALLBACK,
            actual_profile=None,  # type: ignore[arg-type]
            actual_mode=OperatingMode.FAST,
            policy=AutoDetectPolicy.CONFIRM,
        )
    with pytest.raises(ValueError):
        AutoDetectResolution(
            detection_state=DetectionState.DETECTOR_ERROR_FALLBACK,
            actual_profile=SemanticProfile.FEATURE,
            actual_mode=None,  # type: ignore[arg-type]
            policy=AutoDetectPolicy.CONFIRM,
        )


def test_resolution_coerces_string_enums() -> None:
    res = AutoDetectResolution(
        detection_state="recommended",
        actual_profile="feature",
        actual_mode="fast",
        policy="confirm",
        recommended_profile="feature",
        recommended_mode="fast",
        confidence=0.8,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.policy is AutoDetectPolicy.CONFIRM


# ── Detector fakes ──────────────────────────────────────────────────────────


def test_static_detector_returns_fixed_decision() -> None:
    decision = AutoDetectDecision(
        recommended_profile=SemanticProfile.RESEARCH,
        recommended_mode=OperatingMode.FAST,
        confidence=0.6,
    )
    det = StaticWorkKindDetector(decision)
    assert isinstance(det, WorkKindDetector)
    assert det.detect(task="t", project="p") is decision


def test_raising_detector_raises() -> None:
    det = RaisingWorkKindDetector(ValueError("nope"))
    assert isinstance(det, WorkKindDetector)
    with pytest.raises(ValueError, match="nope"):
        det.detect(task="t", project="p")


def test_raising_detector_default_error() -> None:
    with pytest.raises(RuntimeError):
        RaisingWorkKindDetector().detect(task="t", project="p")


# ── AutoDetectConfig: parse / validate ──────────────────────────────────────


def test_config_parse_defaults_on_empty() -> None:
    cfg = AutoDetectConfig.parse({})
    assert cfg.policy is AutoDetectPolicy.CONFIRM
    assert cfg.confidence_threshold == 0.7
    assert cfg.fallback_profile is SemanticProfile.FEATURE
    assert cfg.on_low_confidence is FallbackAction.FALLBACK
    assert cfg.on_error is FallbackAction.FALLBACK


def test_config_parse_none_yields_defaults() -> None:
    cfg = AutoDetectConfig.parse(None)
    assert cfg.policy is AutoDetectPolicy.CONFIRM


def test_config_parse_full() -> None:
    cfg = AutoDetectConfig.parse(
        {
            "policy": "trust_above_threshold",
            "confidence_threshold": 0.85,
            "fallback_profile": "small_task",
            "on_low_confidence": "fail",
            "on_error": "fallback",
        }
    )
    assert cfg.policy is AutoDetectPolicy.TRUST_ABOVE_THRESHOLD
    assert cfg.confidence_threshold == 0.85
    assert cfg.fallback_profile is SemanticProfile.SMALL_TASK
    assert cfg.on_low_confidence is FallbackAction.FAIL


def test_config_ignores_unknown_keys() -> None:
    cfg = AutoDetectConfig.parse({"_comment": "hello", "policy": "confirm"})
    assert cfg.policy is AutoDetectPolicy.CONFIRM


@pytest.mark.parametrize("bad", [-0.1, 1.5, 2, -1])
def test_config_rejects_bad_threshold(bad: float) -> None:
    with pytest.raises(ValueError):
        AutoDetectConfig.parse({"confidence_threshold": bad})


def test_config_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError):
        AutoDetectConfig.parse({"policy": "trust_me_bro"})


def test_config_rejects_unknown_fallback_profile() -> None:
    with pytest.raises(ValueError):
        AutoDetectConfig.parse({"fallback_profile": "auto-detect"})


def test_config_rejects_unknown_fallback_action() -> None:
    with pytest.raises(ValueError):
        AutoDetectConfig.parse({"on_error": "explode"})


# ── AutoDetectConfig: load from AppConfig over config.defaults.json ─────────


def test_config_from_app_config_reads_defaults() -> None:
    # No injection: reads the shipped config.defaults.json auto_detect block
    # through AppConfig.load().pipeline.
    cfg = AutoDetectConfig.from_app_config()
    assert isinstance(cfg, AutoDetectConfig)
    assert cfg.policy is AutoDetectPolicy.CONFIRM
    assert cfg.fallback_profile is SemanticProfile.FEATURE
    assert 0.0 <= cfg.confidence_threshold <= 1.0


def test_config_from_app_config_honors_user_overlay(monkeypatch) -> None:
    # Simulate a user-config overlay landing in AppConfig.pipeline.
    from core.infra import config as _config

    class _FakeAppConfig:
        pipeline = {
            "auto_detect": {
                "policy": "trust_above_threshold",
                "confidence_threshold": 0.95,
                "fallback_profile": "small_task",
            }
        }

        @classmethod
        def load(cls) -> _FakeAppConfig:
            return cls()

    monkeypatch.setattr(_config, "AppConfig", _FakeAppConfig)
    cfg = AutoDetectConfig.from_app_config()
    assert cfg.policy is AutoDetectPolicy.TRUST_ABOVE_THRESHOLD
    assert cfg.confidence_threshold == 0.95
    assert cfg.fallback_profile is SemanticProfile.SMALL_TASK
