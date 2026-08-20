"""Profile phase-effort projection reaches the dispatch slots.

Pins the seam the field report caught missing: ``profiles_v2`` overlays
(written by ``profile customize --phase-effort``) land on
``PhaseStep.effort`` at load time, but dispatch read only the global
``AppConfig.phase_effort_map``. The projection helper plus the
``_synthesize_phase_config`` precedence below close that gap: profile
declaration wins for its phase, global config remains the fallback.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline.project.profile_setup import profile_phase_efforts
from pipeline.runtime import LoopStep, PhaseStep, Profile
from pipeline.runtime.roles import EffortLevel

# ──────────────────────────────────────────────────────────────────────────
# profile_phase_efforts — projection helper
# ──────────────────────────────────────────────────────────────────────────


def _profile(*steps) -> Profile:
    return Profile(name="p", steps=tuple(steps))


class TestProfilePhaseEfforts:
    def test_top_level_step_effort_is_projected(self) -> None:
        prof = _profile(
            PhaseStep(phase="implement", effort=EffortLevel.LOW),
            PhaseStep(phase="review_changes"),
        )
        assert profile_phase_efforts(prof) == {"implement": "low"}

    def test_loop_inner_step_effort_is_projected(self) -> None:
        loop = LoopStep(
            steps=(
                PhaseStep(phase="plan", effort=EffortLevel.LOW),
                PhaseStep(phase="validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=3,
        )
        assert profile_phase_efforts(_profile(loop)) == {"plan": "low"}

    def test_no_declared_effort_yields_empty_map(self) -> None:
        prof = _profile(
            PhaseStep(phase="plan"), PhaseStep(phase="implement"),
        )
        assert profile_phase_efforts(prof) == {}

    def test_duplicate_phase_keeps_last_declaration(self) -> None:
        prof = _profile(
            PhaseStep(phase="plan", effort=EffortLevel.HIGH),
            PhaseStep(phase="plan", effort=EffortLevel.LOW),
        )
        assert profile_phase_efforts(prof) == {"plan": "low"}


# ──────────────────────────────────────────────────────────────────────────
# _synthesize_phase_config — precedence at slot construction
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _Call:
    runtime: str
    model: str
    effort: str | None


class _FakeAgent:
    def __init__(self, runtime: str, model: str, effort: str | None) -> None:
        self.runtime = runtime
        self.model = model
        self.effort = effort
        self.session_id: str | None = None


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    def resolve(self, runtime: str, model: str, *, effort: str | None = None):
        self.calls.append(_Call(runtime, model, effort))
        return _FakeAgent(runtime, model, effort)


def _synthesize(monkeypatch, *, profile_efforts):
    from core.infra import config as core_config
    from pipeline.project.runtime_setup import _synthesize_phase_config

    class _FakeApp:
        phase_runtime_map: dict[str, str] = {}
        phase_model_map: dict[str, str] = {}
        phase_effort_map = {"plan": "medium", "implement": "medium"}

    monkeypatch.setattr(
        core_config.AppConfig, "load", staticmethod(lambda: _FakeApp()),
    )
    provider = _RecordingProvider()
    _synthesize_phase_config(
        None, _provider=provider,
        plan_model="p", implement_model="b",
        repair_model="r", repair_escalation_model="re",
        review_model="rv",
        profile_phase_efforts=profile_efforts,
    )
    return {c.model: c.effort for c in provider.calls}

class TestOverlayToDispatchContract:
    """Writer-to-reader: a ``profiles_v2`` phase-effort overlay (the shape
    ``profile customize --phase-effort`` writes) must survive overlay
    application + profile parse and come out of the projection helper."""

    def test_customize_overlay_reaches_projection(self) -> None:
        from pipeline.profiles.loader import (
            _apply_profile_overlays,
            parse_profile,
        )

        raw = {
            "planning": {
                "description": "plan only",
                "steps": [
                    {"loop": {
                        "max_rounds": 2,
                        "until": "validate_plan.approved",
                        "steps": [
                            {"phase": "plan"},
                            {"phase": "validate_plan"},
                        ],
                    }},
                ],
            },
        }
        _apply_profile_overlays(raw, {"planning": {"plan": {"effort": "low"}}})
        prof = parse_profile("planning", raw["planning"])
        assert profile_phase_efforts(prof) == {"plan": "low"}


class TestProfileEffortPrecedence:
    def test_profile_effort_wins_over_global_config(self, monkeypatch) -> None:
        efforts = _synthesize(monkeypatch, profile_efforts={"plan": "low"})
        assert efforts["p"] == "low"          # profile declaration wins
        assert efforts["b"] == "medium"       # untouched phase keeps config

    def test_absent_profile_map_keeps_global_config(self, monkeypatch) -> None:
        efforts = _synthesize(monkeypatch, profile_efforts=None)
        assert efforts["p"] == "medium"
        assert efforts["b"] == "medium"
