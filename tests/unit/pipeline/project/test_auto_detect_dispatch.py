"""Stage C auto-detect dispatch (T3) — isolated unit tests.

Exercises :func:`pipeline.project.auto_detect.resolve_auto_detect` across every
branch with the deterministic T1 fakes (no network / LLM):

* confirm-accept on a TTY → RECOMMENDED with ``actual_mode == recommended_mode``;
* confirm-override → RECOMMENDED with ``actual_mode == default`` (or explicit
  ``--mode``);
* non-TTY / trusted threshold gating: success → RECOMMENDED, below-threshold →
  LOW_CONFIDENCE_FALLBACK or FAILED, detector error → DETECTOR_ERROR_FALLBACK
  (``recommended_*`` absent, ``error_reason`` set) or FAILED;
* explicit ``--mode`` always beats ``recommended_mode``;
* ``policy == TRUST_ABOVE_THRESHOLD`` never prompts even on a TTY;
* the scoped ``ORCHO_AUTODETECT_DECISION`` env channel sets / restores / deletes
  and a ``None`` resolution is a no-op (no leak).

An autouse guard makes any accidental call to the real provider-backed
detector fail loudly — no unit test may touch it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from pipeline.project.auto_detect import (
    AUTO_DETECT_PROFILE_TOKEN,
    AUTODETECT_DECISION_ENV,
    WORK_MODE_ENV,
    ProviderWorkKindDetector,
    resolution_to_payload,
    resolve_auto_detect,
    scoped_autodetect_decision_env,
)
from pipeline.runtime.run_shape import OperatingMode, SemanticProfile
from pipeline.runtime.semantic_mode_defaults import default_operating_mode
from pipeline.runtime.work_kind_detection import (
    AutoDetectConfig,
    AutoDetectDecision,
    DetectionState,
    RaisingWorkKindDetector,
    StaticWorkKindDetector,
)


@pytest.fixture(autouse=True)
def _forbid_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No unit test may invoke the provider-backed detector."""
    def _boom(self, *, task: str, project: str):  # noqa: ANN001
        raise AssertionError(
            "ProviderWorkKindDetector.detect must never run in unit tests"
        )
    monkeypatch.setattr(ProviderWorkKindDetector, "detect", _boom)


def _config(**overrides) -> AutoDetectConfig:
    base = {
        "policy": "confirm",
        "confidence_threshold": 0.7,
        "fallback_profile": "feature",
        "on_low_confidence": "fallback",
        "on_error": "fallback",
    }
    base.update(overrides)
    return AutoDetectConfig.parse(base)


def _decision(
    profile: str = "migration",
    mode: str = "fast",
    confidence: float = 0.9,
) -> AutoDetectDecision:
    return AutoDetectDecision(
        recommended_profile=profile,
        recommended_mode=mode,
        confidence=confidence,
        rationale="because reasons",
        risk_flags=("schema",),
    )


# ── (a) confirm on a TTY ─────────────────────────────────────────────────────


def test_confirm_accept_uses_recommended_mode() -> None:
    # migration's default mode is pro; the recommendation says fast. Accepting
    # as-is must keep recommended_mode (fast), NOT the work-kind default.
    assert default_operating_mode(SemanticProfile.MIGRATION) is OperatingMode.PRO
    det = StaticWorkKindDetector(_decision("migration", "fast", 0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"), detector=det,
        confirm=lambda d: None,  # accept
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.actual_profile is SemanticProfile.MIGRATION
    assert res.actual_mode is OperatingMode.FAST  # == recommended_mode
    assert res.confirmation_state == "accepted"
    assert res.recommended_profile is SemanticProfile.MIGRATION
    assert res.fallback_used is False


def test_confirm_override_uses_default_mode_of_new_profile() -> None:
    det = StaticWorkKindDetector(_decision("migration", "fast", 0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"), detector=det,
        confirm=lambda d: SemanticProfile.SMALL_TASK,  # override
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.actual_profile is SemanticProfile.SMALL_TASK
    # No explicit --mode → default mode of the chosen profile (small_task=fast).
    assert res.actual_mode is default_operating_mode(SemanticProfile.SMALL_TASK)
    assert res.confirmation_state == "override"
    # The original recommendation is still echoed durably.
    assert res.recommended_profile is SemanticProfile.MIGRATION
    assert res.recommended_mode is OperatingMode.FAST


def test_confirm_override_respects_explicit_mode() -> None:
    det = StaticWorkKindDetector(_decision("migration", "fast", 0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"), detector=det,
        explicit_mode="governed",
        confirm=lambda d: SemanticProfile.SMALL_TASK,
    )
    assert res.actual_profile is SemanticProfile.SMALL_TASK
    assert res.actual_mode is OperatingMode.GOVERNED


def test_trust_policy_on_tty_never_prompts() -> None:
    # policy == TRUST_ABOVE_THRESHOLD must NOT prompt even on a TTY.
    det = StaticWorkKindDetector(_decision("feature", "fast", 0.95))

    def _confirm(_d):
        raise AssertionError("confirm must not be called under TRUST policy")

    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="trust_above_threshold"), detector=det,
        confirm=_confirm,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.confirmation_state == "auto"


# ── (b) trusted / non-interactive threshold gating ───────────────────────────


def test_non_tty_trusted_success_above_threshold() -> None:
    det = StaticWorkKindDetector(_decision("complex_feature", "pro", 0.8))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.actual_profile is SemanticProfile.COMPLEX_FEATURE
    assert res.actual_mode is OperatingMode.PRO  # recommended_mode
    assert res.confirmation_state == "auto"
    assert res.fallback_used is False


def test_threshold_boundary_is_inclusive() -> None:
    det = StaticWorkKindDetector(_decision("feature", "fast", 0.7))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    assert res.detection_state is DetectionState.RECOMMENDED


def test_non_tty_below_threshold_low_confidence_fallback() -> None:
    det = StaticWorkKindDetector(_decision("migration", "governed", 0.3))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(
            confidence_threshold=0.7, fallback_profile="feature",
            on_low_confidence="fallback",
        ),
        detector=det,
    )
    assert res.detection_state is DetectionState.LOW_CONFIDENCE_FALLBACK
    assert res.actual_profile is SemanticProfile.FEATURE  # config.fallback
    assert res.actual_mode is default_operating_mode(SemanticProfile.FEATURE)
    assert res.fallback_used is True
    assert res.fallback_reason  # reason recorded
    # The (rejected) recommendation is still echoed — not passed off as absent.
    assert res.recommended_profile is SemanticProfile.MIGRATION


def test_non_tty_below_threshold_fail() -> None:
    det = StaticWorkKindDetector(_decision("migration", "governed", 0.3))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7, on_low_confidence="fail"),
        detector=det,
    )
    assert res.detection_state is DetectionState.FAILED
    assert res.recommended_profile is SemanticProfile.MIGRATION
    assert res.fallback_reason


def test_low_confidence_fallback_respects_explicit_mode() -> None:
    det = StaticWorkKindDetector(_decision("migration", "governed", 0.2))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(fallback_profile="feature"), detector=det,
        explicit_mode="governed",
    )
    assert res.detection_state is DetectionState.LOW_CONFIDENCE_FALLBACK
    assert res.actual_mode is OperatingMode.GOVERNED


# ── detector error branch ────────────────────────────────────────────────────


def test_detector_error_fallback_omits_recommendation() -> None:
    det = RaisingWorkKindDetector(RuntimeError("boom"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(fallback_profile="feature", on_error="fallback"),
        detector=det,
    )
    assert res.detection_state is DetectionState.DETECTOR_ERROR_FALLBACK
    assert res.recommended_profile is None
    assert res.recommended_mode is None
    assert res.confidence is None
    assert res.error_reason and "boom" in res.error_reason
    assert res.actual_profile is SemanticProfile.FEATURE
    assert res.actual_mode is default_operating_mode(SemanticProfile.FEATURE)
    assert res.fallback_used is True


def test_detector_error_fail() -> None:
    det = RaisingWorkKindDetector(ValueError("nope"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(on_error="fail"), detector=det,
    )
    assert res.detection_state is DetectionState.FAILED
    assert res.recommended_profile is None
    assert res.error_reason and "nope" in res.error_reason


def test_detector_error_in_confirm_branch_falls_back() -> None:
    # A detector exception under the confirm branch also applies on_error.
    det = RaisingWorkKindDetector(RuntimeError("kaboom"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm", on_error="fallback"), detector=det,
        confirm=lambda d: pytest.fail("confirm must not run after error"),
    )
    assert res.detection_state is DetectionState.DETECTOR_ERROR_FALLBACK


# ── explicit --mode precedence ───────────────────────────────────────────────


def test_explicit_mode_always_beats_recommended_mode() -> None:
    det = StaticWorkKindDetector(_decision("feature", "fast", 0.95))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
        explicit_mode="governed",
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.recommended_mode is OperatingMode.FAST
    assert res.actual_mode is OperatingMode.GOVERNED  # explicit --mode wins


# ── detector wiring / manual-profile guard ───────────────────────────────────


def test_detector_invoked_exactly_once() -> None:
    class _Counting:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, *, task: str, project: str) -> AutoDetectDecision:
            self.calls += 1
            return _decision("feature", "fast", 0.9)

    det = _Counting()
    resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(), detector=det,
    )
    assert det.calls == 1


def test_token_is_not_a_profile() -> None:
    # The guard that gates dispatch in cli.main() keys off this token; it must
    # never collide with a real profile (so a manual --profile never matches).
    with pytest.raises(ValueError):
        SemanticProfile(AUTO_DETECT_PROFILE_TOKEN)


def test_provider_detector_construction_is_side_effect_free() -> None:
    # Constructing the provider detector must not resolve a runtime or import
    # provider machinery (detect() is patched to boom by the autouse fixture,
    # proving it is not invoked here).
    det = ProviderWorkKindDetector()
    assert det.runtime == "claude"
    assert det.model is None


# ── scoped env channel ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env() -> Iterator[None]:
    before = os.environ.get(AUTODETECT_DECISION_ENV)
    os.environ.pop(AUTODETECT_DECISION_ENV, None)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(AUTODETECT_DECISION_ENV, None)
        else:
            os.environ[AUTODETECT_DECISION_ENV] = before


def test_scoped_env_sets_and_restores_absent_key() -> None:
    det = StaticWorkKindDetector(_decision("feature", "fast", 0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(), detector=det,
    )
    assert AUTODETECT_DECISION_ENV not in os.environ
    with scoped_autodetect_decision_env(res):
        assert AUTODETECT_DECISION_ENV in os.environ
        import json
        payload = json.loads(os.environ[AUTODETECT_DECISION_ENV])
        assert payload == resolution_to_payload(res)
    # Key was absent before → removed on exit (no leak into a later run).
    assert AUTODETECT_DECISION_ENV not in os.environ


def test_scoped_env_restores_previous_value() -> None:
    os.environ[AUTODETECT_DECISION_ENV] = "prior"
    det = StaticWorkKindDetector(_decision("feature", "fast", 0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(), detector=det,
    )
    with scoped_autodetect_decision_env(res):
        assert os.environ[AUTODETECT_DECISION_ENV] != "prior"
    assert os.environ[AUTODETECT_DECISION_ENV] == "prior"


def test_scoped_env_none_writes_no_channel() -> None:
    # A manual concrete profile leaves resolution None → no channel written.
    with scoped_autodetect_decision_env(None):
        assert AUTODETECT_DECISION_ENV not in os.environ
    assert AUTODETECT_DECISION_ENV not in os.environ


def test_scoped_env_none_clears_stale_decision_during_run() -> None:
    # F2: a stale ORCHO_AUTODETECT_DECISION (left externally or restored from an
    # earlier auto-detect run) must NOT be visible inside a later manual run, or
    # run_setup would persist meta.auto_detect for a run that never used
    # auto-detect. The channel is cleared for the body and restored afterwards.
    os.environ[AUTODETECT_DECISION_ENV] = "stale"
    with scoped_autodetect_decision_env(None):
        assert AUTODETECT_DECISION_ENV not in os.environ
    assert os.environ[AUTODETECT_DECISION_ENV] == "stale"


def test_scoped_env_restored_on_exception() -> None:
    det = StaticWorkKindDetector(_decision("feature", "fast", 0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(), detector=det,
    )
    with pytest.raises(RuntimeError), scoped_autodetect_decision_env(res):
        assert AUTODETECT_DECISION_ENV in os.environ
        raise RuntimeError("boom")
    assert AUTODETECT_DECISION_ENV not in os.environ


# ── scoped work-mode channel (F1) ────────────────────────────────────────────


@pytest.fixture
def _clean_work_mode() -> Iterator[None]:
    before = os.environ.get(WORK_MODE_ENV)
    os.environ.pop(WORK_MODE_ENV, None)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(WORK_MODE_ENV, None)
        else:
            os.environ[WORK_MODE_ENV] = before


def test_scoped_env_sets_work_mode_and_removes_when_absent(
    _clean_work_mode: None,
) -> None:
    # F1: the resolved actual_mode is scoped into ORCHO_WORK_MODE around the run
    # and removed on exit when the key was absent before — so a recommended mode
    # cannot leak into a later manual run in the same process.
    det = StaticWorkKindDetector(_decision("migration", "pro", 0.95))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.5), detector=det,
    )
    assert res.actual_mode == OperatingMode.PRO
    assert WORK_MODE_ENV not in os.environ
    with scoped_autodetect_decision_env(res):
        assert os.environ[WORK_MODE_ENV] == "pro"
    assert WORK_MODE_ENV not in os.environ


def test_scoped_env_restores_previous_work_mode(_clean_work_mode: None) -> None:
    # A previous explicit work-mode is restored verbatim after the run.
    os.environ[WORK_MODE_ENV] = "governed"
    det = StaticWorkKindDetector(_decision("migration", "pro", 0.95))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.5), detector=det,
    )
    with scoped_autodetect_decision_env(res):
        assert os.environ[WORK_MODE_ENV] == "pro"
    assert os.environ[WORK_MODE_ENV] == "governed"


def test_scoped_env_none_leaves_work_mode_untouched(
    _clean_work_mode: None,
) -> None:
    # Manual profile path: the work-mode channel is owned by manual --mode and
    # must not be cleared by the auto-detect scoping.
    os.environ[WORK_MODE_ENV] = "fast"
    with scoped_autodetect_decision_env(None):
        assert os.environ[WORK_MODE_ENV] == "fast"
    assert os.environ[WORK_MODE_ENV] == "fast"


def test_auto_detect_run_then_manual_run_does_not_inherit_mode(
    _clean_work_mode: None,
) -> None:
    # F1 end-to-end at the env level: an auto-detect run that resolves to
    # migration/pro must not leave ORCHO_WORK_MODE set for a subsequent manual
    # run in the SAME process (which carries resolution None). Mirrors the CLI:
    # each run wraps its body in scoped_autodetect_decision_env.
    det = StaticWorkKindDetector(_decision("migration", "pro", 0.95))
    auto_res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.5), detector=det,
    )
    # Run 1 — auto-detect: ORCHO_WORK_MODE is the recommended mode.
    with scoped_autodetect_decision_env(auto_res):
        assert os.environ[WORK_MODE_ENV] == "pro"
    # Between runs the recommended mode is gone again.
    assert WORK_MODE_ENV not in os.environ
    # Run 2 — manual concrete profile (resolution None): no inherited mode and
    # no decision channel, so the default-mode projection picks the profile's
    # own default.
    with scoped_autodetect_decision_env(None):
        assert WORK_MODE_ENV not in os.environ
        assert AUTODETECT_DECISION_ENV not in os.environ


# ── (F3) mock detector path carries the topology recommendation ──────────────


def test_mock_detector_carries_cross_topology_for_wire_task(
    _forbid_real_provider: None,
) -> None:
    """The ``--mock`` CLI detector must merge the deterministic topology axis.

    Regression for F3: a hermetic mock auto-detect run for a core SDK
    wire/MCP-schema task must still resolve to a cross-recommended topology with
    the projected sibling projects, exactly like the provider path — not a bare
    ``mono`` / empty-projects projection. Exercises the real CLI helper so the
    mock and provider topology projections cannot drift.
    """
    from pipeline.project.cli import _build_mock_work_kind_detector
    from pipeline.runtime.run_shape import RunTopology

    task = "Update the core SDK wire schema and the matching MCP tool output."
    detector = _build_mock_work_kind_detector(task)
    res = resolve_auto_detect(
        task=task, project="/p", interactive=False,
        config=_config(policy="trust_above_threshold", confidence_threshold=0.5),
        detector=detector,
    )
    assert res.recommended_topology is RunTopology.CROSS_RECOMMENDED
    assert "orcho-core" in res.delivery_projects
    assert "orcho-mcp" in res.delivery_projects
    assert res.topology_reason
    # The payload round-trips the topology axis for durable meta.auto_detect.
    payload = resolution_to_payload(res)
    assert payload["recommended_topology"] == "cross_recommended"
    assert "orcho-mcp" in payload["delivery_projects"]


def test_mock_detector_mono_for_unrelated_task(
    _forbid_real_provider: None,
) -> None:
    # A task with no cross signals stays mono with empty projected projects.
    from pipeline.project.cli import _build_mock_work_kind_detector
    from pipeline.runtime.run_shape import RunTopology

    task = "Rename a local helper variable for clarity."
    detector = _build_mock_work_kind_detector(task)
    res = resolve_auto_detect(
        task=task, project="/p", interactive=False,
        config=_config(policy="trust_above_threshold", confidence_threshold=0.5),
        detector=detector,
    )
    assert res.recommended_topology is RunTopology.MONO
    assert res.delivery_projects == ()
