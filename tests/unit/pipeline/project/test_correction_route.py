# SPDX-License-Identifier: Apache-2.0
"""``derive_correction_route`` — pure route derivation from triage records.

Covers each triage ``kind`` (code_fix / gate_rerun / contract_ack /
blocked), the None / non-mapping inputs, the unknown-kind defensive
normalization, and the operator-facing reason text.
"""

from __future__ import annotations

import pytest

from pipeline.project.correction_route import (
    SHORTCUT_SKIP_PHASES,
    CorrectionRoute,
    derive_correction_route,
)


def _record(kind: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {"kind": kind, "summary": f"summary for {kind}"}
    base.update(extra)
    return base


def test_code_fix_no_skip_no_halt() -> None:
    route = derive_correction_route(_record("code_fix"))
    assert isinstance(route, CorrectionRoute)
    assert route.kind == "code_fix"
    assert route.skip_phases == frozenset()
    assert route.halt is False
    assert "summary for code_fix" in route.reason


@pytest.mark.parametrize("kind", ["gate_rerun", "contract_ack"])
def test_shortcut_routes_skip_phases_no_halt(kind: str) -> None:
    route = derive_correction_route(_record(kind))
    assert route is not None
    assert route.kind == kind
    assert route.skip_phases == SHORTCUT_SKIP_PHASES
    assert route.halt is False
    assert kind in route.reason
    assert "summary for " + kind in route.reason
    assert "not applicable" in route.reason


def test_blocked_halts_with_blockers_in_reason() -> None:
    route = derive_correction_route(
        _record("blocked", blockers=["missing creds", "needs human"])
    )
    assert route is not None
    assert route.kind == "blocked"
    assert route.halt is True
    assert route.skip_phases == frozenset()
    assert "summary for blocked" in route.reason
    assert "missing creds" in route.reason
    assert "needs human" in route.reason


def test_blocked_without_blockers_still_halts() -> None:
    route = derive_correction_route(_record("blocked"))
    assert route is not None
    assert route.halt is True
    assert "summary for blocked" in route.reason


def test_unknown_kind_normalizes_to_blocked() -> None:
    route = derive_correction_route(_record("nonsense_kind"))
    assert route is not None
    assert route.halt is True
    assert route.skip_phases == frozenset()
    # The offending kind is surfaced in the reason for the operator.
    assert "nonsense_kind" in route.reason
    assert "summary for nonsense_kind" in route.reason


def test_empty_kind_normalizes_to_blocked() -> None:
    route = derive_correction_route({"kind": "", "summary": "no kind given"})
    assert route is not None
    assert route.halt is True
    assert "no kind given" in route.reason


@pytest.mark.parametrize("bad", [None, "not a mapping", 42, ["list"]])
def test_none_or_non_mapping_returns_none(bad: object) -> None:
    assert derive_correction_route(bad) is None  # type: ignore[arg-type]


def test_missing_summary_uses_placeholder() -> None:
    route = derive_correction_route({"kind": "code_fix"})
    assert route is not None
    assert "no triage summary recorded" in route.reason


def test_to_evidence_is_flat_dict() -> None:
    route = derive_correction_route(_record("gate_rerun"))
    assert route is not None
    evidence = route.to_evidence()
    assert evidence["kind"] == "gate_rerun"
    assert evidence["halt"] is False
    assert sorted(evidence["skip_phases"]) == sorted(SHORTCUT_SKIP_PHASES)
    assert isinstance(evidence["skip_phases"], list)
    assert evidence["reason"] == route.reason
