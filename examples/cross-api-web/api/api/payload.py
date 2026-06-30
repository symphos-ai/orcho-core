"""User payload producer for the demo API."""
from __future__ import annotations

USER_FIELDS: tuple[str, ...] = ("user_id", "name", "email")


def build_user_payload(user_id: str, name: str, email: str) -> dict[str, str]:
    """Return the user row payload."""
    return {
        "user_id": user_id,
        "name":    name,
        "email":   email,
    }
