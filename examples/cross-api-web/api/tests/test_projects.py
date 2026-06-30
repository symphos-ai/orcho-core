"""Local tests for the project producer."""
from __future__ import annotations

from api.projects import PROJECT_FIELDS, build_project_payload


def test_project_fields_advertised() -> None:
    assert PROJECT_FIELDS == ("project_id", "name", "team_id", "status")


def test_default_status_is_active() -> None:
    payload = build_project_payload("p-1", "Orcho", "t-1")
    assert payload["status"] == "active"


def test_explicit_status_overrides_default() -> None:
    payload = build_project_payload("p-2", "Archive", "t-1", status="archived")
    assert payload["status"] == "archived"
