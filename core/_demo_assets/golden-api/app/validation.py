"""Sample API payload validator with a known bug.

The orcho golden scenario asks an agent to fix this module: ``validate_payload``
must reject payloads missing required fields (``name``, ``email``) with a 400,
but the current implementation returns 200 for everything. The accompanying
test suite encodes the desired behaviour and currently fails on two cases.
"""
from __future__ import annotations

REQUIRED_FIELDS: tuple[str, ...] = ("name", "email")


def validate_payload(data: dict) -> tuple[int, dict]:
    """Validate an API payload and return ``(status_code, response_body)``.

    BUG: any payload — including ones missing required fields — currently
    returns ``(200, {"ok": True})``. The fix should reject payloads missing
    any of :data:`REQUIRED_FIELDS` with status ``400`` and an ``error``
    message naming the missing field.
    """
    return 200, {"ok": True}
