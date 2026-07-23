"""ADR 0039 extension: post-repair re-verify session continuity + banner.

The runner's re-validating review pass (added on top of ADR 0039's
delayed pause) sets ``state.extras["_review_reverify_resume"] = True``
around the second ``_dispatch_via_fsm(review_step, ...)`` call so that:

  1. ``pipeline.phases.builtin._should_resume(state, "repair_round")``
     returns True for the re-verify pass even though ``round_n == 1``,
     forcing ``continue_session=True`` on the agent invocation — the
     reviewer audits fixes to its own prior findings, dropping that
     history would force a cold re-read of the diff.
  2. ``pipeline.project.profile_dispatch.emit_phase_banner`` surfaces
     a ``(re-verify)`` suffix so the validating pass is visually
     distinguishable from the original review of the same round.

Other call sites (``_should_resume(state, "plan_round")``, banners
for non-review phases) must be unaffected.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from pipeline.phases.builtin import _should_resume
from pipeline.plugins import PluginConfig
from pipeline.project.profile_dispatch import emit_phase_banner
from pipeline.runtime import PipelineState
from pipeline.runtime.roles import SessionInvocationRole


@pytest.fixture(autouse=True)
def _live_output_mode():
    """Pin the full live transcript shape.

    ``summary`` is the default run-output mode (the compact append-only
    arc that collapses phase banners to ``▶ <phase>``); these tests pin
    the full banner label, so force ``live`` and restore afterwards.
    """
    from core.observability import logging as _logging

    before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = before


def _state(**extras: object) -> PipelineState:
    s = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
    for k, v in extras.items():
        s.extras[k] = v
    return s


class TestShouldResumeHonorsReverifyFlag:
    def test_flag_forces_resume_on_repair_round_key_round_1(self) -> None:
        state = _state(
            repair_round=1, _review_reverify_resume=True,
        )
        # Without the flag, round 1 returns False (cold start).
        # With the flag set, the re-verify pass gets continue_session=True.
        assert _should_resume(
            state, role=SessionInvocationRole.REVIEW, round_key="repair_round"
        ) is True

    def test_flag_does_not_leak_to_plan_round_key(self) -> None:
        state = _state(
            plan_round=1, _review_reverify_resume=True,
        )
        # The flag is scoped to the review/repair loop. Plan-loop
        # session resume must not pick it up — otherwise a stale flag
        # left in extras would silently force plan-handler resume.
        assert _should_resume(
            state, role=SessionInvocationRole.PLAN, round_key="plan_round"
        ) is False

    def test_no_flag_keeps_existing_round_threshold(self) -> None:
        state = _state(repair_round=1)
        assert _should_resume(
            state, role=SessionInvocationRole.REVIEW, round_key="repair_round"
        ) is False
        state = _state(repair_round=2)
        assert _should_resume(
            state, role=SessionInvocationRole.REVIEW, round_key="repair_round"
        ) is True


class TestBannerReverifySuffix:
    def test_review_banner_appends_re_verify_when_flag_set(self) -> None:
        state = _state(
            repair_round=1, _review_reverify_resume=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_phase_banner("review_changes", state, terminal=True)
        out = buf.getvalue()
        assert "review_changes -- Round 1 (re-verify)" in out

    def test_review_banner_unchanged_without_flag(self) -> None:
        state = _state(repair_round=1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_phase_banner("review_changes", state, terminal=True)
        out = buf.getvalue()
        assert "review_changes -- Round 1" in out
        assert "(re-verify)" not in out

    def test_suffix_does_not_apply_to_other_phases(self) -> None:
        # If the flag accidentally leaked into a non-review banner
        # render, we must not tag unrelated phases as re-verify.
        state = _state(
            repair_round=1, _review_reverify_resume=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_phase_banner("repair_changes", state, terminal=True)
        out = buf.getvalue()
        assert "(re-verify)" not in out
