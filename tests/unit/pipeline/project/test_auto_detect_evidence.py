"""Stage C auto-detect evidence + DONE summary (T4) — isolated unit tests.

Covers the persistence half of the feature:

* ``run_setup.init_run_session`` reads the scoped ``ORCHO_AUTODETECT_DECISION``
  channel and writes the additive ``meta.auto_detect`` block — only when the
  channel is present, valid, and non-empty;
* a missing / empty / malformed / invariant-violating channel is ignored, so a
  manual run (and a manual run *after* an auto-detect run in the same process)
  carries no ``meta.auto_detect`` (fix F2, read side);
* the DETECTOR_ERROR_FALLBACK shape persists ``detection_state`` /
  ``error_reason`` / ``actual_*`` but no ``recommended_*``;
* the DONE evidence summary emits a compact, state-correct ``Auto-detect:``
  line only when ``meta.auto_detect`` exists.

No real provider/LLM is touched — the env payload is built from the T3
serializer over T1 fakes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from pipeline.plugins import PluginConfig
from pipeline.project.auto_detect import (
    AUTODETECT_DECISION_ENV,
    resolution_to_payload,
    scoped_autodetect_decision_env,
)
from pipeline.project.finalization import (
    _auto_detect_summary_line,
    _render_evidence_summary,
)
from pipeline.project.run_setup import _read_autodetect_meta, init_run_session
from pipeline.runtime.work_kind_detection import (
    AutoDetectPolicy,
    AutoDetectResolution,
    DetectionState,
)


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


def _set_env(resolution: AutoDetectResolution) -> dict:
    payload = resolution_to_payload(resolution)
    os.environ[AUTODETECT_DECISION_ENV] = json.dumps(payload)
    return payload


def _recommended() -> AutoDetectResolution:
    return AutoDetectResolution(
        detection_state=DetectionState.RECOMMENDED,
        actual_profile="feature",
        actual_mode="fast",
        policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
        recommended_profile="feature",
        recommended_mode="fast",
        confidence=0.82,
        rationale="looks like a feature",
        risk_flags=("schema",),
        confirmation_state="auto",
    )


def _detector_error() -> AutoDetectResolution:
    return AutoDetectResolution(
        detection_state=DetectionState.DETECTOR_ERROR_FALLBACK,
        actual_profile="feature",
        actual_mode="fast",
        policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
        fallback_used=True,
        error_reason="RuntimeError: boom",
        fallback_reason="detector error",
    )


def _init_session(tmp_path: Path) -> dict:
    return init_run_session(
        task="do a thing",
        project_path=tmp_path,
        plugin=PluginConfig(),
        model="claude-opus-4-8",
        profile_name="feature",
        session_mode=__import__(
            "agents.protocols", fromlist=["SessionMode"]
        ).SessionMode.AUTO,
        change_handoff="uncommitted",
        output_dir=tmp_path,
        plan_source="local",
        projected_profile=None,
        resume_mode=None,
        followup_parent_run_id=None,
        followup_parent_run_dir=None,
        followup_parent_status=None,
        followup_base_task=None,
        plan_source_run_id=None,
    )


# ── _read_autodetect_meta: presence / absence / validity ─────────────────────


def test_read_returns_payload_when_present() -> None:
    payload = _set_env(_recommended())
    assert _read_autodetect_meta() == payload


def test_read_none_when_absent() -> None:
    assert _read_autodetect_meta() is None


@pytest.mark.parametrize("raw", ["", "   ", "not json", "[]", "123"])
def test_read_none_when_empty_or_malformed(raw: str) -> None:
    os.environ[AUTODETECT_DECISION_ENV] = raw
    assert _read_autodetect_meta() is None


def test_read_none_when_invariant_violated() -> None:
    # A RECOMMENDED payload missing its recommendation violates the resolution
    # invariant; reconstruction raises, so it is rejected, never persisted.
    bad = resolution_to_payload(_recommended())
    bad["recommended_profile"] = None  # illegal for RECOMMENDED
    bad["recommended_mode"] = None
    os.environ[AUTODETECT_DECISION_ENV] = json.dumps(bad)
    assert _read_autodetect_meta() is None


# ── init_run_session: meta.auto_detect persistence ───────────────────────────


def test_meta_block_present_for_auto_detect(tmp_path: Path) -> None:
    payload = _set_env(_recommended())
    session = _init_session(tmp_path)
    assert session["auto_detect"] == payload
    # And it is persisted to meta.json on disk.
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["auto_detect"]["detection_state"] == "recommended"
    assert meta["auto_detect"]["actual_profile"] == "feature"
    assert meta["auto_detect"]["actual_mode"] == "fast"
    assert meta["auto_detect"]["recommended_profile"] == "feature"
    assert meta["auto_detect"]["confidence"] == 0.82


def test_meta_actual_mode_matches_resolution(tmp_path: Path) -> None:
    # actual_mode in meta must equal the mode the run actually starts with.
    res = AutoDetectResolution(
        detection_state=DetectionState.LOW_CONFIDENCE_FALLBACK,
        actual_profile="feature",
        actual_mode="governed",  # explicit --mode wins in this resolution
        policy=AutoDetectPolicy.TRUST_ABOVE_THRESHOLD,
        recommended_profile="migration",
        recommended_mode="pro",
        confidence=0.2,
        fallback_used=True,
        fallback_reason="confidence below threshold",
    )
    _set_env(res)
    session = _init_session(tmp_path)
    assert session["auto_detect"]["actual_mode"] == "governed"


def test_meta_detector_error_omits_recommendation(tmp_path: Path) -> None:
    _set_env(_detector_error())
    session = _init_session(tmp_path)
    block = session["auto_detect"]
    assert block["detection_state"] == "detector_error_fallback"
    assert block["recommended_profile"] is None
    assert block["recommended_mode"] is None
    assert block["confidence"] is None
    assert block["error_reason"] == "RuntimeError: boom"
    assert block["actual_profile"] == "feature"
    assert block["actual_mode"] == "fast"


def test_manual_run_has_no_meta_block(tmp_path: Path) -> None:
    # No env channel → no meta.auto_detect (manual concrete profile).
    session = _init_session(tmp_path)
    assert "auto_detect" not in session
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert "auto_detect" not in meta


def test_f2_no_leak_into_later_manual_run(tmp_path: Path) -> None:
    # KEY F2 test: an auto-detect run then a manual run in the SAME process.
    # The scoped channel is cleared between runs (simulated by popping the env,
    # which the T3 context manager does in finally), so the manual run carries
    # no meta.auto_detect.
    _set_env(_recommended())
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir()
    auto_session = init_run_session(
        task="t", project_path=auto_dir, plugin=PluginConfig(),
        model="m", profile_name="feature",
        session_mode=__import__(
            "agents.protocols", fromlist=["SessionMode"]
        ).SessionMode.AUTO,
        change_handoff="uncommitted", output_dir=auto_dir, plan_source="local",
        projected_profile=None, resume_mode=None, followup_parent_run_id=None,
        followup_parent_run_dir=None, followup_parent_status=None,
        followup_base_task=None, plan_source_run_id=None,
    )
    assert "auto_detect" in auto_session

    # Scoped cleanup between runs.
    os.environ.pop(AUTODETECT_DECISION_ENV, None)

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    manual_session = _init_session(manual_dir)
    assert "auto_detect" not in manual_session
    manual_meta = json.loads(
        (manual_dir / "meta.json").read_text(encoding="utf-8")
    )
    assert "auto_detect" not in manual_meta


def test_f2_stale_decision_env_with_manual_run_via_scoped_cm(
    tmp_path: Path,
) -> None:
    # KEY F2 test wired through the real fix: a VALID stale
    # ORCHO_AUTODETECT_DECISION is present in the environment, but the manual run
    # enters via ``scoped_autodetect_decision_env(None)`` (resolution None — the
    # CLI's manual path). The context manager clears the channel for the run, so
    # run_setup must NOT persist meta.auto_detect, and the DONE line is absent.
    _set_env(_recommended())
    assert _read_autodetect_meta() is not None  # stale value is itself valid

    with scoped_autodetect_decision_env(None):
        # Inside the manual run the channel is invisible.
        assert _read_autodetect_meta() is None
        session = _init_session(tmp_path)

    assert "auto_detect" not in session
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert "auto_detect" not in meta
    assert _auto_detect_summary_line(meta.get("auto_detect")) is None
    # The stale value is restored after the manual run (untouched externally).
    assert _read_autodetect_meta() is not None


# ── DONE summary line ────────────────────────────────────────────────────────


def test_summary_line_accepted_with_confidence() -> None:
    line = _auto_detect_summary_line(
        {
            "detection_state": "recommended",
            "actual_profile": "feature",
            "actual_mode": "fast",
            "confirmation_state": "accepted",
            "confidence": 0.82,
        }
    )
    assert line == "  Auto-detect: feature fast accepted (confidence 0.82)"


def test_summary_line_auto_selected() -> None:
    line = _auto_detect_summary_line(
        {
            "detection_state": "recommended",
            "actual_profile": "complex_feature",
            "actual_mode": "pro",
            "confirmation_state": "auto",
            "confidence": 0.9,
        }
    )
    assert "auto-selected" in line
    assert "complex_feature pro" in line


def test_summary_line_low_confidence_fallback() -> None:
    line = _auto_detect_summary_line(
        {
            "detection_state": "low_confidence_fallback",
            "actual_profile": "feature",
            "actual_mode": "fast",
            "confidence": 0.3,
        }
    )
    assert "low-confidence fallback" in line
    assert "(confidence 0.30)" in line


def test_summary_line_detector_error_has_no_confidence() -> None:
    line = _auto_detect_summary_line(
        {
            "detection_state": "detector_error_fallback",
            "actual_profile": "feature",
            "actual_mode": "fast",
            "confidence": None,
        }
    )
    assert line == "  Auto-detect: feature fast detector-error fallback"
    assert "confidence" not in line


def test_summary_line_none_when_no_block() -> None:
    assert _auto_detect_summary_line(None) is None
    assert _auto_detect_summary_line({}) is None
    assert _auto_detect_summary_line("nope") is None


def test_evidence_summary_includes_line_only_when_present() -> None:
    base = {"phases": {}}
    with_block = {
        "phases": {},
        "auto_detect": {
            "detection_state": "recommended",
            "actual_profile": "feature",
            "actual_mode": "fast",
            "confirmation_state": "accepted",
            "confidence": 0.82,
        },
    }
    manual_lines = _render_evidence_summary(base)
    auto_lines = _render_evidence_summary(with_block)
    assert not any("Auto-detect" in ln for ln in manual_lines)
    assert any("Auto-detect: feature fast accepted" in ln for ln in auto_lines)
