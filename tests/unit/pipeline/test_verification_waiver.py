"""Unit tests for :mod:`pipeline.verification_waiver`.

Covers the provider-neutral durable-waiver reader: gate-command identity from
``gate:<command>:<round>`` handoff ids and explicit ``gate_command`` fields,
list-of-waivers normalisation, exclusion of review/plan waivers, command names
containing ``':'``, and best-effort degradation on malformed input.
"""

from __future__ import annotations

from pipeline.verification_waiver import (
    WAIVER_KEY,
    GateWaiver,
    collect_gate_waivers,
)


def _extras(waiver: object) -> dict:
    return {WAIVER_KEY: waiver}


def test_gate_handoff_id_yields_command_keyed_waiver() -> None:
    waivers = collect_gate_waivers(
        _extras(
            {
                "handoff_id": "gate:broad-non-e2e:1",
                "phase": "final_acceptance",
                "waiver_text": "accepted: pre-existing failures",
                "note": "operator note",
                "decided_by": "operator",
            }
        )
    )

    assert set(waivers) == {"broad-non-e2e"}
    w = waivers["broad-non-e2e"]
    assert isinstance(w, GateWaiver)
    assert w.gate_command == "broad-non-e2e"
    assert w.handoff_id == "gate:broad-non-e2e:1"
    assert w.phase == "final_acceptance"
    assert w.waiver_text == "accepted: pre-existing failures"
    assert w.note == "operator note"
    assert w.decided_by == "operator"


def test_explicit_gate_command_field_wins() -> None:
    waivers = collect_gate_waivers(
        _extras(
            {
                "handoff_id": "gate:ignored:9",
                "gate_command": "ruff-check",
                "waiver_text": "ok",
            }
        )
    )

    assert set(waivers) == {"ruff-check"}
    assert waivers["ruff-check"].gate_command == "ruff-check"


def test_list_of_waivers_is_normalised() -> None:
    waivers = collect_gate_waivers(
        _extras(
            [
                {"handoff_id": "gate:broad-non-e2e:1", "waiver_text": "a"},
                {"handoff_id": "gate:ruff:2", "waiver_text": "b"},
            ]
        )
    )

    assert set(waivers) == {"broad-non-e2e", "ruff"}


def test_review_waiver_is_ignored() -> None:
    waivers = collect_gate_waivers(
        _extras(
            {
                "handoff_id": "review:3",
                "phase": "review_changes",
                "waiver_text": "accepted findings",
            }
        )
    )

    assert waivers == {}


def test_command_with_colon_is_extracted_whole() -> None:
    waivers = collect_gate_waivers(
        _extras({"handoff_id": "gate:pytest:slow:1", "waiver_text": "x"})
    )

    assert set(waivers) == {"pytest:slow"}
    assert waivers["pytest:slow"].handoff_id == "gate:pytest:slow:1"


def test_gate_prefix_without_round_is_ignored() -> None:
    waivers = collect_gate_waivers(
        _extras({"handoff_id": "gate:broad-non-e2e", "waiver_text": "x"})
    )

    assert waivers == {}


def test_extras_take_priority_over_session() -> None:
    extras = _extras(
        {"handoff_id": "gate:broad-non-e2e:2", "waiver_text": "from-extras"}
    )
    session = _extras(
        {"handoff_id": "gate:broad-non-e2e:1", "waiver_text": "from-session"}
    )

    waivers = collect_gate_waivers(extras, session)

    assert waivers["broad-non-e2e"].waiver_text == "from-extras"


def test_session_fallback_when_extras_empty() -> None:
    session = _extras(
        {"handoff_id": "gate:broad-non-e2e:1", "waiver_text": "from-session"}
    )

    waivers = collect_gate_waivers(None, session)

    assert set(waivers) == {"broad-non-e2e"}
    assert waivers["broad-non-e2e"].waiver_text == "from-session"


def test_garbage_and_empty_inputs_degrade_to_empty() -> None:
    assert collect_gate_waivers(None) == {}
    assert collect_gate_waivers({}) == {}
    assert collect_gate_waivers(_extras(None)) == {}
    assert collect_gate_waivers(_extras("not-a-mapping")) == {}
    assert collect_gate_waivers(_extras(12345)) == {}
    assert collect_gate_waivers(_extras({"handoff_id": 999})) == {}
    assert collect_gate_waivers(_extras([1, 2, "x"])) == {}


def test_missing_waiver_text_defaults_to_empty_string() -> None:
    waivers = collect_gate_waivers(
        _extras({"handoff_id": "gate:broad-non-e2e:1"})
    )

    assert waivers["broad-non-e2e"].waiver_text == ""
    assert waivers["broad-non-e2e"].note is None
    assert waivers["broad-non-e2e"].phase is None
    assert waivers["broad-non-e2e"].decided_by is None
