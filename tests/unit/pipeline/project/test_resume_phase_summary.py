# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the resume per-phase summary formatter and its render seam.

Two surfaces:

* :func:`format_resume_phase_summary` — the pure formatter (no I/O). Fed a
  realistic ``session["phases"]`` mapping, it returns a one-line summary or
  ``None`` (fall back to the generic reason).
* ``emit_phase_log_end`` — the render seam. On a resume-skip it prints the
  enriched line + ``(skipped on resume)`` marker; every non-resume skip stays
  byte-identical; the progress.log END outcome is unchanged.
"""

from __future__ import annotations

import pytest

from core.io.ansi import get_color_enabled, set_color_enabled
from core.observability import logging as _obslog
from pipeline.project.profile_dispatch import emit_phase_log_end
from pipeline.project.resume_phase_summary import format_resume_phase_summary
from pipeline.runtime.runner import RESUME_SKIP_REASON
from pipeline.runtime.state import PipelineState

# ── formatter: happy paths (realistic session["phases"] shapes) ────────────


def test_plan_summary_reads_last_attempt() -> None:
    # plan persists as a LIST of per-round attempts; the last is used.
    phases = {
        "plan": [
            {"total_atomic_tasks": 4, "parsed_file_paths": ["a"]},
            {"total_atomic_tasks": 12, "parsed_file_paths": ["a", "b", "c"]},
        ]
    }
    assert format_resume_phase_summary("plan", phases) == "12 atomic tasks, 3 artifacts"


def test_validate_plan_summary_approved() -> None:
    phases = {"validate_plan": [{"approved": True}]}
    assert format_resume_phase_summary("validate_plan", phases) == "approved"


def test_validate_plan_summary_rejected_uses_short_summary() -> None:
    phases = {"validate_plan": [{"approved": False, "short_summary": "plan too vague"}]}
    assert (
        format_resume_phase_summary("validate_plan", phases)
        == "REJECTED — plan too vague"
    )


def test_validate_plan_summary_rejected_falls_back_to_verdict() -> None:
    phases = {"validate_plan": [{"approved": False, "verdict": "REJECTED"}]}
    assert format_resume_phase_summary("validate_plan", phases) == "REJECTED — REJECTED"


def test_implement_summary_tests_pass() -> None:
    phases = {
        "implement": {
            "progress": {"completed": 5, "total": 5},
            "test_result": {"skipped": False, "passed": True, "output": ""},
        }
    }
    assert (
        format_resume_phase_summary("implement", phases)
        == "5/5 subtasks, tests pass"
    )


def test_implement_summary_tests_fail() -> None:
    phases = {
        "implement": {
            "progress": {"completed": 3, "total": 5},
            "test_result": {"skipped": False, "passed": False, "output": "boom"},
        }
    }
    assert (
        format_resume_phase_summary("implement", phases)
        == "3/5 subtasks, tests fail"
    )


def test_implement_summary_tests_na_when_result_absent() -> None:
    # No test gate ran → no ``test_result`` key → n/a (never a crash).
    phases = {"implement": {"progress": {"completed": 2, "total": 2}}}
    assert (
        format_resume_phase_summary("implement", phases)
        == "2/2 subtasks, tests n/a"
    )


@pytest.mark.parametrize("name", ["review_changes", "repair_changes"])
def test_rounds_summary_clean(name: str) -> None:
    # review/repair are folded into the shared ``rounds`` list.
    phases = {"rounds": [{"round": 1, "critique": ""}]}
    assert format_resume_phase_summary(name, phases) == "round 1 — clean"


@pytest.mark.parametrize("name", ["review_changes", "repair_changes"])
def test_rounds_summary_findings(name: str) -> None:
    phases = {
        "rounds": [
            {"round": 1, "critique": "first"},
            {"round": 2, "critique": "still broken"},
        ]
    }
    assert format_resume_phase_summary(name, phases) == "round 2 — findings raised"


def test_final_acceptance_summary_approved_verdict() -> None:
    phases = {"final_acceptance": {"verdict": "APPROVED"}}
    assert (
        format_resume_phase_summary("final_acceptance", phases)
        == "APPROVED, ship-ready"
    )


def test_final_acceptance_summary_approved_ship_ready_flag() -> None:
    phases = {"final_acceptance": {"ship_ready": True}}
    assert (
        format_resume_phase_summary("final_acceptance", phases)
        == "APPROVED, ship-ready"
    )


def test_final_acceptance_summary_rejected_counts_blockers() -> None:
    phases = {
        "final_acceptance": {
            "verdict": "REJECTED",
            "release_blockers": ["missing tests", "no docs"],
        }
    }
    assert (
        format_resume_phase_summary("final_acceptance", phases)
        == "REJECTED — 2 blockers"
    )


# ── formatter: fallback (None) for unknown / missing / partial shapes ───────


@pytest.mark.parametrize(
    "name,phases",
    [
        ("compliance_check", {"compliance_check": {"ok": True}}),  # unknown phase
        ("correction_triage", {"correction_triage": {}}),          # unknown phase
        ("plan", {}),                                              # missing key
        ("plan", {"plan": []}),                                    # empty attempt list
        ("plan", {"plan": [{"parsed_file_paths": ["a"]}]}),        # missing total
        ("validate_plan", {"validate_plan": [{}]}),                # no ``approved``
        ("implement", {"implement": {}}),                          # no ``progress``
        ("implement", {"implement": {"progress": {"completed": 1}}}),  # no total
        ("review_changes", {"rounds": []}),                        # empty rounds
        ("review_changes", {"rounds": [{"critique": "x"}]}),       # no round number
        ("final_acceptance", {"final_acceptance": {"verdict": "PENDING"}}),  # indeterminate
    ],
)
def test_formatter_falls_back_to_none(name: str, phases: dict) -> None:
    assert format_resume_phase_summary(name, phases) is None


def test_formatter_none_phases_returns_none() -> None:
    assert format_resume_phase_summary("implement", None) is None


def test_formatter_never_raises_on_garbage_shape() -> None:
    # Wrong types under the phase keys must degrade to None, not raise.
    assert format_resume_phase_summary("plan", {"plan": "not-a-list"}) is None
    assert format_resume_phase_summary("implement", {"implement": 42}) is None
    assert format_resume_phase_summary("review_changes", {"rounds": "nope"}) is None


# ── render seam: emit_phase_log_end ────────────────────────────────────────


def _state(name: str, reason: str) -> PipelineState:
    st = PipelineState(task="t", project_dir=".", plugin=None)
    st.phase_log[name] = {"skipped": reason}
    return st


@pytest.fixture()
def _quiet_progress_log(tmp_path):
    """Point the progress log at a tmp file so ``log_phase`` is safe + readable."""
    _obslog.set_progress_log(tmp_path)
    yield tmp_path / "progress.log"
    _obslog.set_progress_log(None)


def test_seam_resume_skip_prints_enriched_line(capsys, _quiet_progress_log) -> None:
    st = _state("implement", RESUME_SKIP_REASON)
    phases = {
        "implement": {
            "progress": {"completed": 5, "total": 5},
            "test_result": {"skipped": False, "passed": True, "output": ""},
        }
    }
    emit_phase_log_end("implement", st, terminal=True, phases=phases)
    out = capsys.readouterr().out
    assert "  ↳ implement — 5/5 subtasks, tests pass  (skipped on resume)" in out
    # The bare generic chip must NOT be printed for the enriched phase.
    assert "↳ skipped: completed earlier in this run (resumed)" not in out


def test_seam_non_resume_skip_is_byte_identical(capsys, _quiet_progress_log) -> None:
    # A non-resume reason (e.g. review-clean) prints the exact legacy chip,
    # even when a rehydrated ``phases`` mapping is available.
    st = _state("review_changes", "review clean")
    phases = {"rounds": [{"round": 1, "critique": "ignored here"}]}
    emit_phase_log_end("review_changes", st, terminal=True, phases=phases)
    out = capsys.readouterr().out
    assert out == "  ↳ skipped: review clean\n"


def test_seam_resume_skip_partial_result_falls_back(capsys, _quiet_progress_log) -> None:
    # Resume reason but the persisted result is partial → generic chip, no crash.
    st = _state("implement", RESUME_SKIP_REASON)
    emit_phase_log_end("implement", st, terminal=True, phases={"implement": {}})
    out = capsys.readouterr().out
    assert out == f"  ↳ skipped: {RESUME_SKIP_REASON}\n"


def test_seam_resume_skip_no_phases_falls_back(capsys, _quiet_progress_log) -> None:
    # Direct/old callers pass no ``phases`` → behaviour unchanged.
    st = _state("implement", RESUME_SKIP_REASON)
    emit_phase_log_end("implement", st, terminal=True)
    out = capsys.readouterr().out
    assert out == f"  ↳ skipped: {RESUME_SKIP_REASON}\n"


def test_seam_outcome_and_end_line_unchanged(capsys, _quiet_progress_log) -> None:
    # Render-only change: the progress.log END outcome still carries the raw
    # skip reason (the structural ``phase.end`` signal), unaffected by the
    # enriched terminal chip.
    st = _state("implement", RESUME_SKIP_REASON)
    phases = {"implement": {"progress": {"completed": 5, "total": 5}}}
    emit_phase_log_end("implement", st, terminal=True, phases=phases)
    log_text = _quiet_progress_log.read_text(encoding="utf-8")
    assert "END" in log_text
    assert f"→ skipped: {RESUME_SKIP_REASON}" in log_text


def test_seam_no_ansi_leak_when_color_disabled(capsys, _quiet_progress_log) -> None:
    st = _state("implement", RESUME_SKIP_REASON)
    phases = {"implement": {"progress": {"completed": 5, "total": 5}}}
    prev = get_color_enabled()
    set_color_enabled(False)
    try:
        emit_phase_log_end("implement", st, terminal=True, phases=phases)
    finally:
        set_color_enabled(prev)
    out = capsys.readouterr().out
    assert "\033" not in out
    assert "  ↳ implement — 5/5 subtasks, tests n/a  (skipped on resume)" in out
