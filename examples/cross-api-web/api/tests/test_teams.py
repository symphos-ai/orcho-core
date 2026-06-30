"""Local tests for the team producer."""
from __future__ import annotations

from api.teams import TEAM_FIELDS, build_team_payload


def test_team_fields_advertised() -> None:
    assert TEAM_FIELDS == ("team_id", "name", "owner_id")


def test_build_team_payload_round_trips_inputs() -> None:
    payload = build_team_payload("t-1", "Platform", "u-7")
    assert payload == {
        "team_id":  "t-1",
        "name":     "Platform",
        "owner_id": "u-7",
    }
