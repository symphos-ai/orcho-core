"""Unit coverage for the correction fixed-point guard (ADR 0098).

The module under test is pure: identity normalization plus a conjunctive,
conservative comparison fed the two progress facts by injection. No IO, no
subprocess, no provider — these tests construct plain session dicts.
"""

from __future__ import annotations

from typing import Any

from pipeline.project.correction_fixed_point import (
    FixedPointVerdict,
    blocker_identity_set,
    evaluate_fixed_point,
    render_non_convergence_block,
)


def _rejected_session(
    *,
    release_blockers: list[dict[str, Any]] | None = None,
    verification_gaps: list[dict[str, Any]] | None = None,
    engine_gaps: list[dict[str, Any]] | None = None,
    verdict: str = "REJECTED",
    ship_ready: bool = False,
) -> dict[str, Any]:
    fa: dict[str, Any] = {
        "verdict": verdict,
        "ship_ready": ship_ready,
        "approved": verdict == "APPROVED",
        "release_blockers": release_blockers or [],
        "verification_gaps": verification_gaps or [],
    }
    if engine_gaps is not None:
        fa["engine_backstop"] = {
            "reason": "required_receipts_unproven",
            "gaps": engine_gaps,
        }
    return {"status": "halted", "phases": {"final_acceptance": fa}}


_BLOCKER = {
    "id": "R1",
    "severity": "P0",
    "title": "Mandatory gate is red",
    "file": "pipeline/foo.py",
    "body": "some prose body",
    "why_blocks_release": "would ship without a green gate",
}


# ── identity normalization ────────────────────────────────────────────────


class TestBlockerIdentitySet:
    def test_identical_blockers_yield_equal_sets(self) -> None:
        a = _rejected_session(release_blockers=[dict(_BLOCKER)])
        b = _rejected_session(release_blockers=[dict(_BLOCKER)])
        assert blocker_identity_set(a) == blocker_identity_set(b)
        assert blocker_identity_set(a)  # non-empty

    def test_reworded_prose_does_not_change_identity(self) -> None:
        # body / why_blocks_release are prose — they must not enter the key.
        changed = dict(_BLOCKER, body="entirely different explanation")
        assert blocker_identity_set(
            _rejected_session(release_blockers=[dict(_BLOCKER)]),
        ) == blocker_identity_set(
            _rejected_session(release_blockers=[changed]),
        )

    def test_different_file_changes_key(self) -> None:
        other = dict(_BLOCKER, file="pipeline/bar.py")
        assert blocker_identity_set(
            _rejected_session(release_blockers=[dict(_BLOCKER)]),
        ) != blocker_identity_set(
            _rejected_session(release_blockers=[other]),
        )

    def test_different_severity_changes_key(self) -> None:
        other = dict(_BLOCKER, severity="P2")
        assert blocker_identity_set(
            _rejected_session(release_blockers=[dict(_BLOCKER)]),
        ) != blocker_identity_set(
            _rejected_session(release_blockers=[other]),
        )

    def test_verification_gap_uses_required_check(self) -> None:
        gap_a = {
            "risk": "could ship without a green gate",
            "missing_evidence": "pytest exited 1",
            "required_check": "python -m pytest",
        }
        gap_b = dict(gap_a, required_check="python -m ruff check .")
        assert blocker_identity_set(
            _rejected_session(verification_gaps=[gap_a]),
        ) != blocker_identity_set(
            _rejected_session(verification_gaps=[gap_b]),
        )

    def test_engine_backstop_receipts_enter_identity(self) -> None:
        engine_gap = {
            "risk": "required gate 'lint' unproven: receipt missing",
            "missing_evidence": "no passing receipt for lint",
            "required_check": "python -m ruff check .",
        }
        ids = blocker_identity_set(
            _rejected_session(engine_gaps=[engine_gap]),
        )
        assert any("engine_gap" in key for key in ids)

    def test_robust_to_missing_fields_and_empty(self) -> None:
        assert blocker_identity_set(None) == frozenset()
        assert blocker_identity_set({}) == frozenset()
        assert blocker_identity_set({"phases": {}}) == frozenset()
        # A blocker missing file/severity is still keyed, not raised on.
        ids = blocker_identity_set(
            _rejected_session(release_blockers=[{"id": "R9", "title": "x"}]),
        )
        assert ids


# ── evaluate_fixed_point ──────────────────────────────────────────────────


class TestEvaluateFixedPoint:
    def test_equal_blockers_no_progress_is_fixed_point(self) -> None:
        parent = _rejected_session(release_blockers=[dict(_BLOCKER)])
        child = _rejected_session(release_blockers=[dict(_BLOCKER)])
        verdict = evaluate_fixed_point(
            parent, child, code_changed=False, receipts_changed=False,
        )
        assert isinstance(verdict, FixedPointVerdict)
        assert verdict.is_fixed_point is True
        assert verdict.repeated  # the shared identity is reported
        assert verdict.reason

    def test_one_blocker_fixed_is_not_fixed_point(self) -> None:
        # Child resolved R1 but still rejects on R2 — the identity set changed,
        # so this is progress, not a fixed point.
        second = dict(_BLOCKER, id="R2", title="other blocker", file="b.py")
        parent = _rejected_session(
            release_blockers=[dict(_BLOCKER), second],
        )
        child = _rejected_session(release_blockers=[second])
        verdict = evaluate_fixed_point(
            parent, child, code_changed=False, receipts_changed=False,
        )
        assert verdict.is_fixed_point is False

    def test_code_changed_suppresses_guard(self) -> None:
        parent = _rejected_session(release_blockers=[dict(_BLOCKER)])
        child = _rejected_session(release_blockers=[dict(_BLOCKER)])
        verdict = evaluate_fixed_point(
            parent, child, code_changed=True, receipts_changed=False,
        )
        assert verdict.is_fixed_point is False
        assert "diff" in verdict.reason

    def test_receipts_changed_suppresses_guard(self) -> None:
        # Same missing-receipt blockers, but fresh passing receipts produced.
        engine_gap = {
            "risk": "required gate 'lint' unproven: receipt missing",
            "missing_evidence": "no passing receipt for lint",
            "required_check": "python -m ruff check .",
        }
        parent = _rejected_session(engine_gaps=[engine_gap])
        child = _rejected_session(engine_gaps=[engine_gap])
        verdict = evaluate_fixed_point(
            parent, child, code_changed=False, receipts_changed=True,
        )
        assert verdict.is_fixed_point is False
        assert "receipt" in verdict.reason

    def test_non_rejected_parent_is_not_fixed_point(self) -> None:
        parent = {"status": "done", "phases": {}}
        child = _rejected_session(release_blockers=[dict(_BLOCKER)])
        verdict = evaluate_fixed_point(
            parent, child, code_changed=False, receipts_changed=False,
        )
        assert verdict.is_fixed_point is False

    def test_non_rejected_child_is_not_fixed_point(self) -> None:
        parent = _rejected_session(release_blockers=[dict(_BLOCKER)])
        child = {"status": "done", "phases": {}}
        verdict = evaluate_fixed_point(
            parent, child, code_changed=False, receipts_changed=False,
        )
        assert verdict.is_fixed_point is False

    def test_empty_identity_sets_are_not_fixed_point(self) -> None:
        # Both rejected but with no structured blockers: nothing to repeat.
        parent = _rejected_session()
        child = _rejected_session()
        verdict = evaluate_fixed_point(
            parent, child, code_changed=False, receipts_changed=False,
        )
        assert verdict.is_fixed_point is False


# ── render_non_convergence_block ──────────────────────────────────────────


class TestRenderNonConvergenceBlock:
    def test_block_contains_all_required_parts(self) -> None:
        block = render_non_convergence_block(
            repeated=("final_acceptance|release_blocker|r1|p0|foo.py",),
            parent_run_id="20260619_parent",
            child_run_id="20260619_child",
        )
        assert block.startswith("Correction is not converging.")
        assert "Repeated blockers:" in block
        assert "final_acceptance|release_blocker|r1|p0|foo.py" in block
        assert "20260619_parent" in block
        assert "20260619_child" in block
        assert "No relevant blocker evidence changed" in block
        assert (
            "retry with new instructions, approve/waive, or halt" in block
        )

    def test_empty_repeated_renders_placeholder(self) -> None:
        block = render_non_convergence_block(
            repeated=(),
            parent_run_id="p",
            child_run_id="c",
        )
        assert "Repeated blockers: (none)" in block
