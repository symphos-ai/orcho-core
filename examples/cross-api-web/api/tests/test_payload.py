"""Local tests for the user payload producer."""
from __future__ import annotations

from api.payload import USER_FIELDS, build_user_payload


def test_payload_contains_declared_fields() -> None:
    payload = build_user_payload("u1", "Ada", "ada@example.com")
    assert set(payload.keys()) == set(USER_FIELDS)


def test_payload_carries_provided_values() -> None:
    payload = build_user_payload("u1", "Ada", "ada@example.com")
    assert payload["user_id"] == "u1"
    assert payload["name"] == "Ada"
    assert payload["email"] == "ada@example.com"
