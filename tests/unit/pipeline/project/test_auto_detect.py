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
    WORK_MODE_ENV,
    ProviderWorkKindDetector,
    TopologyChoice,
    _extract_json_object,
    _parse_detection_reply,
    apply_topology_choice,
    default_confirm,
    resolution_to_payload,
    resolve_auto_detect,
    scoped_autodetect_decision_env,
)
from pipeline.runtime.run_shape import (
    DeliveryScope,
    OperatingMode,
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

# Captured at import time, *before* the autouse ``_forbid_real_provider`` guard
# patches ``detect`` to raise. The provider-detector tests below call this real
# implementation directly (with a fake agent) instead of going through the
# instance attribute the guard has replaced.
_REAL_DETECT = ProviderWorkKindDetector.detect


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


def _plain_decision(
    profile: str = "complex_feature",
    mode: str = "pro",
    confidence: float = 0.9,
    rationale: str = "because reasons",
) -> AutoDetectDecision:
    """A mono decision (no topology echo) for confirm / threshold branches."""
    return AutoDetectDecision(
        recommended_profile=profile,
        recommended_mode=mode,
        confidence=confidence,
        rationale=rationale,
        risk_flags=("r",),
    )


# ── (d) interactive confirm: accept / override / string-override ─────────────


def test_confirm_accept_returns_recommended_with_accepted_state() -> None:
    """confirm_fn -> None accepts as-is: RECOMMENDED + confirmation 'accepted'.

    Covers the ``use_confirm`` branch (interactive + policy==CONFIRM) and the
    ``override is None`` accept path (auto_detect.py 252-259).
    """
    det = StaticWorkKindDetector(_plain_decision(profile="complex_feature"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"),
        detector=det, confirm=lambda decision: None,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.confirmation_state == "accepted"
    assert res.actual_profile is SemanticProfile.COMPLEX_FEATURE
    assert res.actual_mode is OperatingMode.PRO  # recommended_mode honoured


def test_confirm_override_with_profile_object_records_override() -> None:
    """confirm_fn -> SemanticProfile overrides the recommendation.

    Covers the override branch where ``override`` is already a SemanticProfile
    (auto_detect.py 260-277): detection_state stays RECOMMENDED, actual_profile
    is the override, confirmation_state == 'override', and the recommended_*
    echo still reports the detector's original pick.
    """
    det = StaticWorkKindDetector(_plain_decision(profile="complex_feature"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"),
        detector=det, confirm=lambda decision: SemanticProfile.RESEARCH,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.confirmation_state == "override"
    assert res.actual_profile is SemanticProfile.RESEARCH
    # actual_mode is the override profile's default (research -> fast), not the
    # detector's recommended_mode.
    assert res.actual_mode is OperatingMode.FAST
    assert res.recommended_profile is SemanticProfile.COMPLEX_FEATURE
    assert res.fallback_used is False


def test_confirm_override_with_string_is_coerced_to_profile() -> None:
    """confirm_fn -> str is coerced via SemanticProfile(override).

    Covers the ``else SemanticProfile(override)`` coercion arm
    (auto_detect.py 261-262) when the confirm callback returns a raw name.
    """
    det = StaticWorkKindDetector(_plain_decision())
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"),
        detector=det, confirm=lambda decision: "refactor",
    )
    assert res.actual_profile is SemanticProfile.REFACTOR
    assert res.confirmation_state == "override"


def test_confirm_default_used_when_callback_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With policy==CONFIRM + interactive and no confirm arg, default_confirm runs.

    Covers ``confirm_fn = ... else default_confirm`` (auto_detect.py 253): an
    empty line on the real default prompt accepts the recommendation.
    """
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    det = StaticWorkKindDetector(_plain_decision(profile="feature"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=True,
        config=_config(policy="confirm"),
        detector=det,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.confirmation_state == "accepted"
    assert res.actual_profile is SemanticProfile.FEATURE


# ── (e) explicit --mode threading ────────────────────────────────────────────


def test_explicit_mode_overrides_recommended_on_accept() -> None:
    """explicit_mode wins over recommended_mode on an accepted recommendation.

    Covers _explicit_mode coercion (auto_detect.py 89) and the forced arm of
    _mode_for_accept: a trusted auto-accept with --mode governed yields
    actual_mode==GOVERNED even though the decision recommended 'pro'.
    """
    det = StaticWorkKindDetector(_plain_decision(mode="pro", confidence=0.9))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        explicit_mode="governed",
        config=_config(confidence_threshold=0.7), detector=det,
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.confirmation_state == "auto"
    assert res.actual_mode is OperatingMode.GOVERNED


# ── (f) detector on_error == FAIL ────────────────────────────────────────────


def test_detector_error_with_on_error_fail_returns_failed() -> None:
    """detector raise + on_error=FAIL -> FAILED with error_reason, no fallback.

    Distinct from test_detector_error_resolution_keeps_mono_defaults (which
    covers the FALLBACK arm): this exercises auto_detect.py 231-240.
    """
    det = RaisingWorkKindDetector(RuntimeError("boom"))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(on_error="fail"), detector=det,
    )
    assert res.detection_state is DetectionState.FAILED
    assert res.fallback_used is False
    assert res.error_reason == "RuntimeError: boom"
    assert res.fallback_reason == "detector error and on_error=fail"
    assert res.actual_profile is SemanticProfile.FEATURE  # config fallback


# ── (g) low-confidence threshold gating ──────────────────────────────────────


def test_low_confidence_with_on_low_confidence_fail_returns_failed() -> None:
    """confidence < threshold + on_low_confidence=FAIL -> FAILED.

    Covers auto_detect.py 286-306: fallback_profile is used, the recommended_*
    echo is preserved, and fallback_reason names the failed low-confidence gate.
    """
    det = StaticWorkKindDetector(_plain_decision(profile="research", confidence=0.4))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7, on_low_confidence="fail"),
        detector=det,
    )
    assert res.detection_state is DetectionState.FAILED
    assert res.fallback_used is False
    assert res.actual_profile is SemanticProfile.FEATURE
    assert res.recommended_profile is SemanticProfile.RESEARCH
    assert "on_low_confidence=fail" in res.fallback_reason


def test_low_confidence_default_falls_back() -> None:
    """confidence < threshold (default fallback) -> LOW_CONFIDENCE_FALLBACK.

    Covers auto_detect.py 307-321: fallback_used True, confirmation_state
    'auto', and fallback_reason carrying the confidence/threshold comparison.
    """
    det = StaticWorkKindDetector(_plain_decision(profile="research", confidence=0.4))
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7, on_low_confidence="fallback"),
        detector=det,
    )
    assert res.detection_state is DetectionState.LOW_CONFIDENCE_FALLBACK
    assert res.fallback_used is True
    assert res.confirmation_state == "auto"
    assert res.actual_profile is SemanticProfile.FEATURE
    assert "0.4" in res.fallback_reason and "0.7" in res.fallback_reason


# ── (h) config / detector default construction ───────────────────────────────


def test_config_and_detector_default_to_real_constructors() -> None:
    """config=None / detector=None build the real defaults (auto_detect.py 216,218).

    With both omitted, AutoDetectConfig.from_app_config() supplies the config
    and the default ProviderWorkKindDetector is constructed; the autouse guard
    makes its detect() raise, which the dispatcher turns into the on_error
    fallback (DETECTOR_ERROR_FALLBACK) — proving the default detector was
    actually built and invoked rather than an injected fake.
    """
    res = resolve_auto_detect(task="t", project="/p", interactive=False)
    assert res.detection_state is DetectionState.DETECTOR_ERROR_FALLBACK
    assert "AssertionError" in res.error_reason


# ── (i) _restore_env: previous-value restore arm ─────────────────────────────


def test_scoped_env_restores_previous_decision_value() -> None:
    """A pre-existing decision env is restored on scope exit (auto_detect.py 391).

    Sets ORCHO_AUTODETECT_DECISION before entering the scope so the finally
    branch takes the ``previous is not None`` path and writes it back.
    """
    det = StaticWorkKindDetector(_cross_decision())
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=_config(confidence_threshold=0.7), detector=det,
    )
    os.environ[AUTODETECT_DECISION_ENV] = "PREVIOUS"
    previous_mode = os.environ.get(WORK_MODE_ENV)
    os.environ[WORK_MODE_ENV] = "PREVMODE"
    try:
        with scoped_autodetect_decision_env(res):
            assert os.environ[AUTODETECT_DECISION_ENV] != "PREVIOUS"
        assert os.environ[AUTODETECT_DECISION_ENV] == "PREVIOUS"
        assert os.environ[WORK_MODE_ENV] == "PREVMODE"
    finally:
        if previous_mode is None:
            os.environ.pop(WORK_MODE_ENV, None)
        else:
            os.environ[WORK_MODE_ENV] = previous_mode


# ── (j) default_confirm prompt loop ──────────────────────────────────────────


def _sequenced_input(*replies: str):
    """A fake ``input`` yielding each reply in turn; raises StopIteration after."""
    it = iter(replies)
    return lambda _prompt: next(it)


def test_default_confirm_empty_input_accepts() -> None:
    """Empty line -> None (accept). Covers auto_detect.py 460-467 + rationale print."""
    dec = _plain_decision(profile="feature", rationale="a short reason")
    import builtins

    real_input = builtins.input
    builtins.input = lambda _prompt: ""
    try:
        assert default_confirm(dec) is None
    finally:
        builtins.input = real_input


def test_default_confirm_valid_work_kind_returns_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid work-kind name -> that SemanticProfile (auto_detect.py 468-469)."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "research")
    dec = _plain_decision(profile="feature")
    assert default_confirm(dec) is SemanticProfile.RESEARCH


def test_default_confirm_invalid_then_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown input re-prompts (470) then an empty line accepts (-> None)."""
    monkeypatch.setattr("builtins.input", _sequenced_input("not-a-kind", ""))
    dec = _plain_decision(profile="feature", rationale="")
    assert default_confirm(dec) is None


def test_default_confirm_eof_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOFError from input -> None (auto_detect.py 463-465)."""
    def _raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    assert default_confirm(_plain_decision()) is None


def test_default_confirm_keyboard_interrupt_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt from input -> None (auto_detect.py 463-465)."""
    def _raise_kbd(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise_kbd)
    assert default_confirm(_plain_decision()) is None


def test_default_confirm_exhausts_retries_then_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three invalid entries exhaust the loop and fall through to None (471)."""
    monkeypatch.setattr("builtins.input", _sequenced_input("x", "y", "z"))
    assert default_confirm(_plain_decision()) is None


# ── (k) ProviderWorkKindDetector.detect + reply parsing ──────────────────────


class _FakeAgent:
    """Minimal architect stand-in: records invoke() and returns a canned reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple] = []

    def invoke(self, prompt, project, *, mutates_artifacts):  # noqa: ANN001
        self.calls.append((prompt, project, mutates_artifacts))
        return self.reply


class _FakeRegistry:
    def __init__(self, agent: _FakeAgent) -> None:
        self.agent = agent

    def architect(self, model, runtime, *, effort=None):  # noqa: ANN001
        return self.agent


def _patch_registry(monkeypatch: pytest.MonkeyPatch, agent: _FakeAgent) -> None:
    from agents.registry import AgentRegistry

    monkeypatch.setattr(
        AgentRegistry, "default", staticmethod(lambda: _FakeRegistry(agent))
    )


def test_provider_detect_parses_reply_and_merges_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detect() parses a fenced reply and merges the deterministic topology.

    Covers auto_detect.py 495-519 (lazy registry resolution, prompt build,
    parse, topology merge). The agent is faked; no LLM/network. The semantic
    pick comes from the model reply, the topology axis from recommend_topology.
    """
    reply = (
        'prelude ```json\n'
        '{"recommended_profile": "feature", "recommended_mode": "pro", '
        '"confidence": 0.83, "rationale": "looks like a feature", '
        '"risk_flags": ["schema"]}\n``` trailer'
    )
    agent = _FakeAgent(reply)
    _patch_registry(monkeypatch, agent)
    det = ProviderWorkKindDetector(model="some-model")

    decision = _REAL_DETECT(det, task="add a button", project="/p")

    assert decision.recommended_profile is SemanticProfile.FEATURE
    assert decision.recommended_mode is OperatingMode.PRO
    assert decision.confidence == pytest.approx(0.83)
    expected = recommend_topology("add a button")
    assert decision.recommended_topology is expected.topology
    assert decision.delivery_projects == expected.projects
    assert decision.topology_reason == expected.reason
    # The detection call must be declared read-only.
    assert agent.calls and agent.calls[0][2] is False


def test_provider_detect_resolves_model_from_config_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model=None falls back to AppConfig.phase_model_map (auto_detect.py 498-500)."""
    reply = (
        '```json\n{"recommended_profile": "small_task", '
        '"recommended_mode": "fast", "confidence": 0.5}\n```'
    )
    agent = _FakeAgent(reply)
    _patch_registry(monkeypatch, agent)
    det = ProviderWorkKindDetector()  # model unset -> config lookup

    decision = _REAL_DETECT(det, task="tiny", project="/p")
    assert decision.recommended_profile is SemanticProfile.SMALL_TASK


def test_parse_detection_reply_fenced_array_is_not_an_object() -> None:
    """A fenced JSON array -> ValueError 'not a JSON object' (auto_detect.py 549-550)."""
    reply = "```json\n[1, 2, 3]\n```"
    with pytest.raises(ValueError, match="not a JSON object"):
        _parse_detection_reply(reply)


def test_parse_detection_reply_missing_key_raises_keyerror() -> None:
    """A payload missing recommended_profile -> KeyError (auto_detect.py 552)."""
    reply = '```json\n{"recommended_mode": "pro", "confidence": 0.5}\n```'
    with pytest.raises(KeyError):
        _parse_detection_reply(reply)


def test_extract_json_object_bare_braces() -> None:
    """A bare (unfenced) object is extracted from surrounding prose (562-571)."""
    obj = _extract_json_object('noise {"recommended_profile": "feature"} tail')
    assert obj == {"recommended_profile": "feature"}


def test_extract_json_object_no_object_raises() -> None:
    """No braces and no fence -> ValueError 'no JSON object' (auto_detect.py 567-570)."""
    with pytest.raises(ValueError, match="no JSON object"):
        _extract_json_object("there is no json here")
