"""Test suite encoding the desired ``validate_payload`` behaviour.

The golden scenario expects an agent to make these tests pass without
weakening the assertions. The two failing tests are the runnable
acceptance criteria a real fix must satisfy.
"""
from __future__ import annotations

from app.validation import validate_payload


def test_valid_payload_returns_200() -> None:
    status, body = validate_payload({"name": "Alice", "email": "a@b.c"})
    assert status == 200
    assert body == {"ok": True}


def test_missing_name_returns_400() -> None:
    status, body = validate_payload({"email": "a@b.c"})
    assert status == 400
    assert "name" in body.get("error", "")


def test_missing_email_returns_400() -> None:
    status, body = validate_payload({"name": "Bob"})
    assert status == 400
    assert "email" in body.get("error", "")


def test_empty_payload_returns_400() -> None:
    status, body = validate_payload({})
    assert status == 400
    assert "error" in body
