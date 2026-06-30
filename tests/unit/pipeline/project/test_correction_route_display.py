# SPDX-License-Identifier: Apache-2.0
"""Pure formatting of the correction-route decision / summary lines.

Covers every triage ``kind`` (code_fix / gate_rerun / contract_ack /
blocked), the None / non-mapping inputs, the unknown-kind defensive blocked
equivalent, summary truncation, and the ``final_acceptance`` outcome mapping
(ok / rejected / pending) used by the DONE-block summary line.
"""

from __future__ import annotations

import pytest

from pipeline.project.correction_route_display import (
    CorrectionRouteDisplay,
    format_correction_route_decision,
    format_correction_route_summary,
)


def _record(kind: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {"kind": kind, "summary": f"summary for {kind}"}
    base.update(extra)
    return base


# --- decision line ---------------------------------------------------------


def test_decision_none_for_non_mapping() -> None:
    assert format_correction_route_decision(None) is None
    assert format_correction_route_decision("not a mapping") is None  # type: ignore[arg-type]
    assert format_correction_route_decision(["x"]) is None  # type: ignore[arg-type]


def test_decision_code_fix_full_path() -> None:
    display = format_correction_route_decision(_record("code_fix"))
    assert isinstance(display, CorrectionRouteDisplay)
    assert display.kind == "code_fix"
    assert display.halted is False
    assert display.text == "Correction route: code_fix → full correction path"


@pytest.mark.parametrize("kind", ["gate_rerun", "contract_ack"])
def test_decision_shortcut_lists_sorted_skip_phases(kind: str) -> None:
    display = format_correction_route_decision(_record(kind))
    assert display is not None
    assert display.kind == kind
    assert display.halted is False
    # Sorted order, identical to CorrectionRoute.to_evidence()'s sorted list.
    assert display.text == (
        f"Correction route: {kind} → skipping "
        "implement/repair_changes/review_changes"
    )


def test_decision_blocked_includes_summary_and_blocker() -> None:
    record = _record(
        "blocked",
        summary="contract drift unresolved",
        blockers=["missing API token", "schema mismatch"],
    )
    display = format_correction_route_decision(record)
    assert display is not None
    assert display.kind == "blocked"
    assert display.halted is True
    assert display.text.startswith(
        "Correction route: blocked → halting before implement; "
    )
    assert "contract drift unresolved" in display.text
    assert "2 blockers; first: missing API token" in display.text


def test_decision_blocked_without_blockers() -> None:
    display = format_correction_route_decision(
        _record("blocked", summary="cannot proceed safely")
    )
    assert display is not None
    assert display.halted is True
    assert "cannot proceed safely" in display.text
    assert "blocker" not in display.text


def test_decision_unknown_kind_is_blocked_equivalent() -> None:
    display = format_correction_route_decision(_record("weird-kind"))
    assert display is not None
    # Defensive normalization in derive_correction_route keeps the raw kind
    # but routes it through the halting branch.
    assert display.kind == "weird-kind"
    assert display.halted is True
    assert "halting before implement" in display.text


def test_decision_long_summary_is_truncated() -> None:
    long_summary = "x" * 400
    display = format_correction_route_decision(
        _record("blocked", summary=long_summary)
    )
    assert display is not None
    # textwrap.shorten caps the reason; the full 400-char body must not leak.
    assert long_summary not in display.text
    assert "…" in display.text


# --- summary line ----------------------------------------------------------


def test_summary_none_for_non_mapping() -> None:
    assert format_correction_route_summary(None) is None
    assert format_correction_route_summary("nope") is None  # type: ignore[arg-type]


def test_summary_none_without_triage_record() -> None:
    assert format_correction_route_summary({"implement": {}}) is None
    assert format_correction_route_summary({"correction_triage": "x"}) is None


def _stamped(kind: str, *, skip: list[str], halt: bool) -> dict[str, object]:
    return {
        "kind": kind,
        "summary": f"summary for {kind}",
        "route": {
            "kind": kind,
            "skip_phases": skip,
            "halt": halt,
            "reason": "stamped reason",
        },
    }


@pytest.mark.parametrize("kind", ["gate_rerun", "contract_ack"])
def test_summary_shortcut_outcome_ok(kind: str) -> None:
    phases = {
        "correction_triage": _stamped(
            kind,
            skip=["implement", "repair_changes", "review_changes"],
            halt=False,
        ),
        "final_acceptance": {"verdict": "APPROVED"},
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert display.kind == kind
    assert display.halted is False
    assert display.text == (
        f"Correction route: {kind} → skipped "
        "implement/repair_changes/review_changes; final_acceptance=ok"
    )


def test_summary_shortcut_outcome_ok_via_ship_ready() -> None:
    phases = {
        "correction_triage": _stamped(
            "gate_rerun",
            skip=["implement", "review_changes"],
            halt=False,
        ),
        "final_acceptance": [{"ship_ready": True}],
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert display.text.endswith("final_acceptance=ok")


def test_summary_shortcut_outcome_rejected() -> None:
    phases = {
        "correction_triage": _stamped(
            "contract_ack", skip=["implement"], halt=False
        ),
        "final_acceptance": {"verdict": "REJECTED", "ship_ready": False},
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert display.text.endswith("final_acceptance=rejected")


def test_summary_shortcut_outcome_pending_when_absent() -> None:
    phases = {
        "correction_triage": _stamped(
            "gate_rerun", skip=["implement"], halt=False
        ),
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert display.text.endswith("final_acceptance=pending")


def test_summary_code_fix_full_path() -> None:
    phases = {
        "correction_triage": _stamped("code_fix", skip=[], halt=False),
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert display.kind == "code_fix"
    assert display.halted is False
    assert display.text == "Correction route: code_fix → full correction path"


def test_summary_blocked_derived_from_unstamped_record() -> None:
    # The blocked / halted path does not stamp a route dict; the summary must
    # derive the route from the raw triage record (halted=True).
    phases = {
        "correction_triage": {
            "kind": "blocked",
            "halted": True,
            "summary": "unsafe to continue",
            "blockers": ["needs human sign-off"],
        },
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert display.kind == "blocked"
    assert display.halted is True
    assert "halted before implement" in display.text
    assert "unsafe to continue" in display.text
    assert "1 blocker: needs human sign-off" in display.text


def test_summary_blocked_truncates_long_reason() -> None:
    phases = {
        "correction_triage": {
            "kind": "blocked",
            "halted": True,
            "summary": "y" * 400,
        },
    }
    display = format_correction_route_summary(phases)
    assert display is not None
    assert "y" * 400 not in display.text
    assert "…" in display.text
